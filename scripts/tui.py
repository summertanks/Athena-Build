#  Copyright (c) 2023. Harkirat S Virk <harkiratsvirk@gmail.com>
#
#  This program comes with ABSOLUTELY NO WARRANTY; for details see COPYING.
#  This is free software, and you are welcome to redistribute it under certain conditions; see COPYING for details.

"""
The tui module sets up the curses interface for executing commands for Athena-Build System.
Simplest use is to create an instance of Tui and call run() on it. Only create one instance of Tui in an application

The base tui only enable commands 'demo', 'clear' & 'quit'. For additional commands please register functions with
register(function name, callable function)
"""

import curses
import signal
import time
import psutil
import queue
import threading
from math import floor


# TODO: make class single instance only
class Tui:
    """Tui Class
    The tui class provides a basic curses based interface. The interface has two parts - console & shell (footer)
    The console ha seen divided into to tabs - one for console output typically written by the print function and second
    is the log tab, written to by the __log__ function. Separate error, warning & info functions are provided for ease.

    The user can execute 'registered' functions by their designated moniker. e.g. tui.register('clear', self._clear)
    will enable the user to call the self._clear() function using the command 'clear'. Any additional command line
    parameters provided will automatically be passed to the function (without sanity check)

    Args:
        banner(str): Accepts a  string which it prints in the bottom of the footer, may be trimmed based on screen size

    Attributes:
        _banner(str): stores the banner on instance creation. Cannot be changed later
        _psutil(__Lockable): Maintains the current string description of resource utilisation, atomic is used correctly
        _tabs({}): collection of tabs - tuple of name, window, buffer, cursor position
        __footer(curses.newwin): holds curses window for the static portion of the tui which includes commandline shell


        __registered_cmd({}): array of commands registered, indexed by commands and the corresponding function
        __cmd_mode: defines the current mode the keystrokes are to be displayed CMD_MODE_NORMAL or CMD_MODE_PASSWORD
        __prompt_str(str): Holds the descriptor string prompt functions are called with
        __cmd_cursor(int): cursor position for shell/prompt incase it exceeds screen width

        __prompt_lock, __shell_lock, __print_lock, __refresh_lock, __log_lock, __widget_lock(threading.Lock) : enables
            atomic functions on these sections

        __dispatch_queue(queue.LifoQueue): dispatch queue for handling keystrokes
        __input_queue(queue.LifoQueue): waiting queue for user inputs

        __widget({}): For running list of widget, progressbar, spinner, etc.

        _stdscr: holding instance of curses.initscr

        self.__setup__()
    Examples:
    """

    class __Lockable:
        def __init__(self, var_type):
            self.__lock = threading.Lock()
            self.__var = var_type()

        @property
        def value(self):
            with self.__lock:
                return self.__var

        @value.setter
        def value(self, var):
            assert isinstance(self.__var, type(var)), 'Lockable object cannot be recast'
            with self.__lock:
                self.__var = var

    class __Spinner:

        # Can pick more from
        # https://stackoverflow.com/questions/2685435/cooler-ascii-spinners
        ASCII_CHAR = ['⣾', '⣽', '⣻', '⢿', '⡿', '⣟', '⣯', '⣷']

        def __init__(self, message):
            self.__message = message[:70]
            self.__lock = threading.Lock()
            self.__position = 0
            self.__running = True

            threading.Thread(target=self.__step__, daemon=True).start()

        def __step__(self):
            while self.__running:
                time.sleep(0.1)
                with self.__lock:
                    self.__position = (self.__position + 1) % len(self.ASCII_CHAR)

        def done(self):
            self.__running = False

        def message(self) -> str:
            return self.__message

        def __str__(self) -> str:
            with self.__lock:
                return self.__message + ' ' + self.ASCII_CHAR[self.__position]

    class __ProgressBar:
        RUNNING = 1
        PAUSED = 2
        STOPPED = 3

        def __init__(self, message, itr_label='it/s', scale_factor=None, bar_width=40):
            self.__label = message[:20]
            self.__value = 0
            self.__max = 100
            self.__itr_label = itr_label[:6]

            self.__time = time.time_ns()
            self.__state = self.RUNNING

            if scale_factor not in [None, 'K', 'M', 'G']:
                scale_factor = None
            self.__scale_factor = scale_factor

            if bar_width < 40:
                bar_width = 40
            elif bar_width > 60:
                bar_width = 60
            self.__bar_width = bar_width

        def __str__(self) -> str:

            completed = floor((self.__value / self.__max) * self.__bar_width)
            remaining = self.__bar_width - completed
            string = self.__label + '[' + '#' * completed + '-' * remaining + '] '
            string += '(' + str(self.__value) + '/' + str(self.__max) + ')'
            delta = int(time.time_ns() - self.__time)
            rate = (self.__value / delta) * 10e8

            factor = 1
            scale_factor = ''
            # if None, Autoscale
            if self.__scale_factor is None:
                if rate > 10e3 * 2:
                    scale_factor = 'K'
                if rate > 10e6 * 2:
                    scale_factor = 'M'
                if rate > 10e9 * 2:
                    scale_factor = 'G'
            else:
                scale_factor = self.__scale_factor

            if scale_factor == 'K':
                factor = 10e3
            elif scale_factor == 'M':
                factor = 10e6
            elif scale_factor == 'G':
                factor = 10e9

            rate = round(rate / factor, 2)

            string += str(rate) + scale_factor + self.__itr_label

            return string

        def step(self, value=1):
            # don't react on stopped
            if self.__state != self.RUNNING:
                return

            self.__value += value
            if self.__value >= self.__max:
                self.__value = self.__max
                self.__state = self.STOPPED

        def max(self, value=None):
            if not value:
                value = 100

            self.__max = value
            # self.step(0)

        def label(self, message):
            self.__label = message

        def close(self):
            pass

        def reset(self):
            self.__value = 0
            self.__time = time.time_ns()

    class __Commands:
        """ Holding class related to command(s)
        Attributes:
            current(str): current command being typed by the user, could be for prompt or shell.
            prompt(str): the prompt shown before uer input

            _history([str]): list holds all previous commands executed
            _registered({}): array of registered commands where key is command and value is the function being invoked
            _mode(bool) : sets whether input should be in clear or masked, e.g. for passwords
            _cursor(int): reference for where to print input from if the input exceed screen width
        """
        CMD_MODE_NORMAL = False
        CMD_MODE_PASSWORD = True

        def __init__(self, instance):
            """
            Basic Init
            Args:
                instance: Accepts the instance of Tui class to call logging related functions
            """
            # let's not pass something else
            assert isinstance(instance, Tui)

            self.tui = instance
            self.current = ''
            self.prompt = ''

            self._history = []
            self._registered = {}
            self._mode = self.CMD_MODE_NORMAL
            self._cursor = 0

        def register_command(self, command_name: str, function, tooltip=''):
            if command_name.strip() == '':
                self.tui.ERROR('Registering Empty Command')
                return
            elif command_name in self._registered:
                self.tui.INFO(f'Registering duplicate command {command_name}, Ignored')
                return
            else:
                self._registered[command_name] = (function, tooltip)

        def get_command(self, command_name: str):
            command_name = command_name.strip()
            if not command_name:
                return None

            if command_name not in self._registered:
                return None

            return self._registered[command_name][0]

        def inc_cursor(self):
            self._cursor = self._cursor + 1

        def dec_cursor(self):
            if self._cursor > 0:
                self._cursor = self._cursor - 1

        def reset_cursor(self):
            self._cursor = 0

        @property
        def cursor(self) -> int:
            return self._cursor

        def set_mask_mode(self):
            self._mode = self.CMD_MODE_PASSWORD

        def reset_mask_mode(self):
            self._mode = self.CMD_MODE_NORMAL

        def is_masked(self):
            return self._mode

        @property
        def history(self) -> []:
            return self._history

        def add_history(self, command: str):
            command = command.strip()
            if command:
                self._history.append(command)

    BOX_WIDTH = 1

    SPINNER_START = 1
    SPINNER_STOP = 2

    PROMPT_YESNO = 1
    PROMPT_INPUT = 2
    PROMPT_OPTIONS = 3
    PROMPT_PASSWORD = 4

    SEVERITY_ERROR = 1
    SEVERITY_WARNING = 2
    SEVERITY_INFO = 3

    COLOR_NORMAL = 1
    COLOR_REVERSE = 2
    COLOR_WARNING = 3
    COLOR_ERROR = 4
    COLOR_HIGHLIGHT = 5
    COLOR_FOOTER = 6

    CMD_MODE_NORMAL = 1
    CMD_MODE_PASSWORD = 2

    bgColor = curses.COLOR_BLACK
    bgFooter = curses.COLOR_BLUE
    fgColor = curses.COLOR_WHITE
    warningColor = curses.COLOR_YELLOW
    errorColor = curses.COLOR_RED
    highlightColor = curses.COLOR_GREEN

    CMD_PROMPT = '$ '

    def __init__(self, banner: str):
        """
        Initialises instance of Tui, involving
            - Setting up internal parameters
            - sets up curses, including checking resolution
            - Creating instances of Tabs & footer (curses.window)
            - registers internal commands, e.g. clear, demo etc.
            - kicks off the shell thread (also thread for getting system utilisation)
        Args:
            banner: accepts str which will be printed in the footer
        """

        # Banner String, needs to be trimmed, currently static
        banner_trim = 30
        self._banner = banner[:banner_trim]

        # Set up the for running the __res_util as a parallel thread
        self._psutil = self.__Lockable(str)
        threading.Thread(target=self._res_util, daemon=True).start()

        # collection of tabs - tuple of name, window, buffer, cursor position
        self._tabs = {}

        # footer defined separately
        self.__footer = None

        # Commands
        self.__cmd = self.__Commands(self)
        self.__cmd.reset_mask_mode()
        self.__cmd.reset_cursor()

        self.__prompt_lock = threading.Lock()
        self.__shell_lock = threading.Lock()
        self.__print_lock = threading.Lock()
        self.__refresh_lock = threading.Lock()
        self.__log_lock = threading.Lock()
        self.__widget_lock = threading.Lock()

        # setting up dispatch queue for handling keystrokes
        self.__dispatch_queue = queue.LifoQueue()
        self.__input_queue = queue.LifoQueue()

        # For running list of widget
        self.__widget = {}

        # let's set up the curses default window
        self._stdscr = curses.initscr()
        self._is_setup = False
        self._setup()

        # set minimum to 80x25 screen, if lesser better to print weird rather than bad calculations
        self.__resolution = {'x': max(curses.COLS, 80), 'y': max(curses.LINES, 25)}

        # Set footer bar size, typically its one for tabs, one for prompt, one for application info,
        # and one each side for the box
        self.__footer_height = 5

        # calculate layout (width, height, origin y, origin x) with origin on top left corner
        self.__tab_coordinates = {'h': self.__resolution['y'] - self.__footer_height, 'w': self.__resolution['x'],
                                  'y': 0, 'x': 0}
        self.__footer_coordinates = {'h': self.__footer_height, 'w': self.__resolution['x'],
                                     'y': self.__resolution['y'] - self.__footer_height, 'x': 0}

        # creating footer, Cant create tab before that
        self.__footer = curses.newwin(self.__footer_coordinates['h'], self.__footer_coordinates['w'],
                                      self.__footer_coordinates['y'], self.__footer_coordinates['x'])

        # Validation
        assert self.__footer_height >= 5, 'TUI: Malformed Footer Size'
        assert self.__footer is not None, "TUI: Footer not defined"

        # creating basic tabs
        self.addtab("console")
        self.addtab("log")

        # set the default tab
        self.__activate__('console')

        # Validation
        assert len([__tab for __tab in self._tabs if __tab in ['console', 'log']]) == 2, 'TUI: Mandatory tabs missing'
        assert len([self._tabs[tab] for tab in self._tabs if self._tabs[tab]['selected']]) == 1, \
            'TUI: Tab not activated correctly'

        self.__log__(self.SEVERITY_INFO, "Initialising TUI environment")
        self.__refresh__()
        self._stdscr.nodelay(True)
        self._stdscr.refresh()

        # Register the signal handler for SIGINT (Ctrl+C)
        signal.signal(signal.SIGINT, self._shutdown)

        self.__cmd.register_command('clear', self.clear)
        self.__cmd.register_command('demo', self.demo)
        self.__cmd.register_command('history', self.history)

        # start the command
        threading.Thread(target=self.shell, daemon=True).start()

    def __refreshfooter__(self):
        tab_tooltip = "Use Tab to rotate through Tabs"
        tab_prefix = 'Tabs:'
        tab_psutil = self._psutil.value

        # print tab list & tooltip
        self.__footer.erase()
        self.__footer.bkgd(curses.color_pair(self.COLOR_FOOTER))
        self.__footer.box()
        self.__footer.addstr(2, self.__resolution['x'] - len(tab_tooltip) - self.BOX_WIDTH, tab_tooltip)
        self.__footer.addstr(3, self.__resolution['x'] - len(tab_psutil) - self.BOX_WIDTH, tab_psutil, curses.A_BOLD)

        self.__footer.addstr(3, self.BOX_WIDTH, self._banner, curses.A_BOLD)

        # Displaying scrollable commandline
        # Usable space = screen width - 2 * BOX_WIDTH
        # Layout < [Box Width] [PROMPT] [One Spaces] [CMD] >
        # Less command prompt should be trimmed to 50% of the available space
        # Remain Command should be scrollable, cursor position defines from where the command is printed

        width = self.__resolution['x'] - 2 * self.BOX_WIDTH

        # Trim to maximum length of 50% width
        cmd_prompt = self.CMD_PROMPT[:floor(width / 2)]

        # Available width - less length of cmd prompt and two (one for seperator, and another cursor)
        width = width - len(cmd_prompt) - 2

        # build cmd
        if not self.__cmd.is_masked():
            cmd = self.__cmd.current
        else:  # CMD_MODE_PASSWORD
            cmd = '*' * len(self.__cmd.current)

        cmd = cmd[self.__cmd.cursor:width + self.__cmd.cursor]

        command_string = cmd_prompt + ' ' + cmd
        self.__footer.addstr(1, self.BOX_WIDTH, command_string)
        self.__footer.addstr('_', curses.A_BLINK | curses.A_BOLD)

        # we should have written till
        self.__footer.addstr(2, self.BOX_WIDTH, tab_prefix)
        index = self.BOX_WIDTH + len(tab_prefix)

        for tab in self._tabs:
            label = ' | ' + tab + ' | '
            if self._tabs[tab]['selected']:
                self.__footer.addstr(2, index, label, curses.A_REVERSE)
            else:
                self.__footer.addstr(2, index, label)
            index += len(label)

        self.__footer.refresh()

    def __refreshtab__(self):
        # printing the Active tab only
        active_tab = self.__activetab__

        buffer = active_tab['buffer']
        window = active_tab['win']
        cursor = min(active_tab['cursor'], len(buffer))
        window.erase()
        for i in range(cursor):
            line = buffer[i]
            window.addstr(line[0], line[1])

        with self.__widget_lock:
            for widget in self.__widget:
                line = str(self.__widget[widget]) + '\n'
                window.addstr(line, curses.A_BOLD)

        window.refresh()

    def __refresh__(self, force=False):
        if force:
            self._stdscr.touchwin()
        with self.__refresh_lock:
            self.__refreshfooter__()
            self.__refreshtab__()
            self._stdscr.refresh()

    @property
    def __activetab__(self) -> {}:
        active_tab = [self._tabs[tab] for tab in self._tabs if self._tabs[tab]['selected']]
        assert len(active_tab) == 1, 'TUI: Active State for tabs inconsistent'
        active_tab = active_tab[0]
        return active_tab

    def _res_util(self):
        while True:
            # Get CPU usage as a percentage
            cpu_percent = psutil.cpu_percent(interval=2)

            # Get memory usage statistics
            mem = psutil.virtual_memory()
            mem_percent = mem.percent

            # Get disk usage statistics
            disk = psutil.disk_usage('/')
            disk_percent = disk.percent

            # String for results
            self._psutil.value = f'CPU: {cpu_percent}% RAM: {mem_percent}% Disk(/): {disk_percent}%'

    def __activate__(self, name):
        assert name in self._tabs, 'TUI: Activating non available Tab'

        for tab in self._tabs:
            self._tabs[tab]['selected'] = False

        self._tabs[name]['selected'] = True
        self.__refresh__()

    def __resizeScreen__(self):
        # calculate layout (width, height, origin y, origin x) with origin on top left corner
        self.__tab_coordinates = {'h': self.__resolution['y'] - self.__footer_height, 'w': self.__resolution['x'],
                                  'y': 0, 'x': 0}
        self.__footer_coordinates = {'h': self.__footer_height, 'w': self.__resolution['x'],
                                     'y': self.__resolution['y'] - self.__footer_height, 'x': 0}
        self.__refresh__()

    def __log__(self, severity, message):
        assert (severity in [self.SEVERITY_ERROR, self.SEVERITY_WARNING, self.SEVERITY_INFO]), \
            f'TUI: Incorrect Severity {severity} defined'

        with self.__log_lock:
            attribute = curses.color_pair(self.COLOR_NORMAL)
            if severity == self.SEVERITY_ERROR:
                attribute = curses.color_pair(self.COLOR_ERROR)
            elif severity == self.SEVERITY_WARNING:
                attribute = curses.color_pair(self.COLOR_WARNING)

            logger = self._tabs['log']
            logger['buffer'].append((message + '\n', attribute))
            logger['cursor'] = len(logger['buffer'])

    def _shutdown(self):
        # if not previously setup - skip
        if not self._is_setup:
            return

        # BEGIN ncurses shutdown/de-initialization...
        # Turn off cbreak mode...
        curses.nocbreak()

        # Turn echo back on.
        curses.echo()

        # Restore cursor blinking.
        curses.curs_set(True)

        # Turn off the keypad...
        self._stdscr.keypad(False)

        # Restore Terminal to original state.
        curses.endwin()

        # END ncurses shutdown/de-initialization...
        self._is_setup = False

    def _setup(self):
        # if already setup dont execute again
        if self._is_setup:
            return

        # BEGIN ncurses startup/initialization...

        # disable echos
        curses.noecho()

        # Enter non-blocking or cbreak mode
        curses.cbreak()

        # Turn off blinking cursor
        curses.curs_set(False)

        # Enable color if we can
        if curses.has_colors():
            curses.start_color()

        # Enable the keypad - also permits decoding of multibyte key sequences,
        self._stdscr.keypad(True)

        # Set color pairs
        # TODO: load from tui.conf else use defaults

        curses.init_pair(self.COLOR_NORMAL, self.fgColor, self.bgColor)
        curses.init_pair(self.COLOR_REVERSE, self.bgColor, self.fgColor)
        curses.init_pair(self.COLOR_WARNING, self.warningColor, self.bgColor)
        curses.init_pair(self.COLOR_ERROR, self.errorColor, self.bgColor)
        curses.init_pair(self.COLOR_HIGHLIGHT, self.highlightColor, self.bgColor)
        curses.init_pair(self.COLOR_FOOTER, self.fgColor, self.bgFooter)

        # END ncurses startup/initialization...
        self._is_setup = True

    def addtab(self, name: str):
        # Strip whitespaces
        name = name.strip()

        # Should not already exist
        assert name not in self._tabs, f'TUI: Attempted creating tab with name "{name}" which already exists'

        if name != '':
            # Tab is a tuple of name, window, buffer, cursor position, and selected state
            self._tabs[name] = {'win': curses.newwin(self.__tab_coordinates['h'], self.__tab_coordinates['w'],
                                                     self.__tab_coordinates['y'], self.__tab_coordinates['x']),
                                'buffer': [], 'cursor': 0, 'selected': True}
            # #nabling Scrolling
            self._tabs[name]['win'].scrollok(True)

    def enabletab(self, name):
        # Set current Tab based on name, provided it is valid
        if name in self._tabs:
            self.__activate__(name)

    def enable_next_tab(self):
        # dict are not sorted, so there is no order.
        active_tab = [tab for tab in self._tabs if self._tabs[tab]['selected']]
        assert len(active_tab) == 1, 'TUI: More than one tab active'
        tab_list = list(self._tabs)
        try:
            next_tab = tab_list[tab_list.index(active_tab[0]) + 1]
        except (ValueError, IndexError):
            next_tab = tab_list[0]

        self.__activate__(next_tab)

    def run(self):
        __quit = False
        # main loop
        while not __quit:
            self.__refresh__()

            # get input
            try:
                c = self._stdscr.getkey()
            except curses.error:
                c = None

            activetab = self.__activetab__
            if c is not None:

                if c == 'KEY_UP':
                    if activetab['cursor'] > self.__tab_coordinates['h']:
                        activetab['cursor'] -= 1
                        continue

                elif c == 'KEY_DOWN':
                    activetab['cursor'] = min(len(activetab['buffer']), activetab['cursor'] + 1)
                    continue

                elif c == 'KEY_RIGHT':
                    width = self.__resolution['x'] - 2 * self.BOX_WIDTH
                    width = width - len(self.CMD_PROMPT[:floor(width / 2)]) - 2
                    # Only if the input is greater than the available space is cursor position relevant
                    if len(self.__cmd.current) > width:
                        if self.__cmd.cursor < len(self.__cmd.current) - width:
                            self.__cmd.inc_cursor()

                elif c == 'KEY_LEFT':
                    self.__cmd.dec_cursor()

                elif c == 'KEY_BACKSPACE':
                    self.__cmd.current = self.__cmd.current[:-1]
                    if self.__cmd.cursor > 0:
                        self.__cmd.dec_cursor()
                    continue

                elif c == '\t':
                    # switch to next Tab on Alt
                    self.enable_next_tab()
                    continue

                # Simple hack - if it is longer than a char it is a special key string
                if len(c) > 1:
                    continue

                # here onwards we are processing keys outside control keys
                # if nothing is waiting in queue don't process any keys
                if self.__dispatch_queue.empty():
                    continue

                # Newline received, based on data input mode the dispatch sequence is identified
                if c == '\n':
                    # Command has been completed
                    if not self.__cmd.current.strip() == '':
                        # Special Case
                        if self.__cmd.current.strip() in ['quit', 'exit', 'q']:
                            __quit = True
                            continue
                        else:
                            self.__input_queue.put(self.__cmd.current)
                            self.__cmd._history.append(self.__cmd.current)
                            try:
                                condition = self.__dispatch_queue.get()
                            except queue.Empty:
                                self.ERROR('TUI: Nothing in dispatch queue')
                                continue

                            with condition:
                                condition.notify()

                    self.__cmd.current = ''
                    self.__cmd.reset_cursor()

                else:
                    self.__cmd.current = ''.join([self.__cmd.current, c])
                    width = self.__resolution['x'] - 2 * self.BOX_WIDTH
                    width = width - len(self.CMD_PROMPT[:floor(width / 2)]) - 2
                    # Only if the input is greater than the available space is cursor position relevant
                    if len(self.__cmd.current) > width:
                        if self.__cmd.cursor < len(self.__cmd.current) - width:
                            self.__cmd.inc_cursor()

            else:
                curses.napms(1)  # wait 1ms to avoid 100% CPU usage

        # clean up
        self._shutdown()

    def INFO(self, message):
        self.__log__(self.SEVERITY_INFO, message)

    def WARNING(self, message):
        self.__log__(self.SEVERITY_WARNING, message)

    def ERROR(self, message):
        self.__log__(self.SEVERITY_ERROR, message)

    def print(self, message, attribute=None):

        with self.__print_lock:
            if attribute is None:
                attribute = curses.color_pair(self.COLOR_NORMAL)
            console = self._tabs['console']

            console['buffer'].append((message + '\n', attribute))
            console['cursor'] = len(console['buffer'])

    def clear(self, name):
        if name == 'all':
            for tab in self._tabs:
                self._tabs[tab]['buffer'] = []
                self._tabs[tab]['cursor'] = 0
        elif name in self._tabs:
            self._tabs[name]['buffer'] = []
            self._tabs[name]['cursor'] = 0
        else:
            self.print(f'Attempted to clear non-existent tab {name}')

    def history(self):
        # The last command will be 'history'
        for cmd in self.__cmd.history[:-1]:
            self.print(cmd)

    def shell(self):
        with self.__shell_lock:

            while True:
                self.CMD_PROMPT = '$'

                condition = threading.Condition()
                self.__dispatch_queue.put(condition)

                with condition:
                    condition.wait()

                try:
                    command = self.__input_queue.get()
                    command = command.strip()
                except queue.Empty:
                    self.ERROR('TUI: Condition called but nothing in Input Stack')
                    continue

                self.print(command, curses.color_pair(self.COLOR_HIGHLIGHT))

                self.CMD_PROMPT = '(command under progress)'
                self.INFO(f'Executing "{command}"')

                cmd_parts = command.split()
                if len(cmd_parts) > 0:
                    function_name = cmd_parts[0]
                    function_args = cmd_parts[1:]

                    function = self.__cmd.get_command(function_name)

                    if not function:
                        self.print(f'Command {function_name} not found')
                        continue

                    try:
                        function(*function_args)
                    except TypeError as e:
                        self.print(f"Error: {e}")
                        self.ERROR(f"Error: {e}")

    def prompt(self, prompt_type, message, options=None) -> str:
        if options is None:
            options = []

        assert prompt_type in [self.PROMPT_YESNO, self.PROMPT_OPTIONS, self.PROMPT_PASSWORD, self.PROMPT_INPUT], \
            f'TUI: Unknown prompt type given'

        if prompt_type == self.PROMPT_OPTIONS:
            assert len(options) > 0, 'Prompt type PROMPT_OPTIONS called without options'

        with self.__prompt_lock:
            old_prompt = self.CMD_PROMPT
            self.CMD_PROMPT = message

            if prompt_type == self.PROMPT_YESNO:
                self.CMD_PROMPT += ' (y/n):'
            elif prompt_type == self.PROMPT_OPTIONS:
                self.CMD_PROMPT += str(options)

            answer = ''
            while True:
                condition = threading.Condition()
                self.__dispatch_queue.put(condition)

                if prompt_type == self.PROMPT_PASSWORD:
                    self.__cmd.set_mask_mode()

                with condition:
                    condition.wait()

                try:
                    answer = self.__input_queue.get()
                except queue.Empty:
                    self.ERROR('TUI: Condition called but nothing in Input Stack')

                if prompt_type == self.PROMPT_OPTIONS and answer not in options:
                    self.print('TUI Prompt: Only answers within the option provided are permitted')
                    continue

                if prompt_type == self.PROMPT_YESNO and answer not in ['y', 'Y', 'n', 'N', 'yes', 'Yes', 'no', 'No']:
                    self.print('TUI Prompt: Only answers related to yes/no are permitted')
                    continue

                break

            if prompt_type == self.PROMPT_PASSWORD:
                self.__cmd.reset_mask_mode()
                self.print(self.CMD_PROMPT + ' ' + '*' * len(answer))
            else:
                self.print(self.CMD_PROMPT + ' ' + answer)
            self.CMD_PROMPT = old_prompt

        return answer

    def spinner(self, message) -> int:
        spin = Tui.__Spinner(message)
        widget_id = spin.__hash__()
        with self.__widget_lock:
            self.__widget[widget_id] = spin
        return widget_id

    def s_stop(self, widget_id: int):
        with self.__widget_lock:
            if widget_id not in self.__widget:
                self.print(f'TUI: No Widget by id {widget_id}')
                return
            self.print(self.__widget[widget_id].message() + '... Done')
            self.__widget.pop(widget_id)

    def progressbar(self, message, itr_label='it/s', scale_factor='', bar_width=40, maxvalue=None) -> int:
        bar = Tui.__ProgressBar(message, itr_label, scale_factor, bar_width)
        if maxvalue and maxvalue > 0:
            bar.max(maxvalue)
        widget_id = bar.__hash__()
        with self.__widget_lock:
            self.__widget[widget_id] = bar
        return widget_id

    def p_step(self, widget_id: int, value: int = 1):
        with self.__widget_lock:
            if widget_id not in self.__widget:
                self.print(f'TUI: No Widget by id {widget_id}')
                return

            bar = self.__widget[widget_id]
            bar.step(value)

    def p_close(self, widget_id: int):
        with self.__widget_lock:
            if widget_id not in self.__widget:
                self.print(f'TUI: No Widget by id {widget_id}')
                return
            self.print(str(self.__widget[widget_id]))
            self.__widget.pop(widget_id)

    def demo(self):

        spin = self.spinner('Starting Demo')
        self.prompt(self.PROMPT_YESNO, 'This is YES NO prompt')
        self.prompt(self.PROMPT_INPUT, 'This accepts Input string')
        self.prompt(self.PROMPT_OPTIONS, 'This allows you to select from options', ['yes', 'no'])
        self.prompt(self.PROMPT_PASSWORD, 'This accepts masked input')

        bar = self.progressbar('Progress Bar Demo', maxvalue=100)

        for i in range(100):
            self.p_step(bar, value=1)
            curses.napms(100)
        self.p_close(bar)

        self.s_stop(spin)


# Main function
if __name__ == '__main__':
    tui = Tui("Athena Build Environment v0.1")
    tui.run()
