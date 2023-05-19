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
from typing import Optional

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
        _footer(curses.newwin): holds curses window for the static portion of the tui which includes commandline shell

        _prompt_lock, _shell_lock, _print_lock, _refresh_lock, _log_lock, _widget_lock(threading.Lock) : enables
            atomic functions on these sections

        _dispatch_queue(queue.LifoQueue): dispatch queue for handling keystrokes
        _input_queue(queue.LifoQueue): waiting queue for user inputs

        _widget({}): For running list of widget, progressbar, spinner, etc.

        _stdscr: holding instance of curses.initscr

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
        """ Internal class for Spinner
        Presents a spinner with given character sequence
        Attributes:
            _message(str): the message printed as action of spinner, trimmed to 70 characters
            _lock: threading lock to keep changes atomic
            _position(int): index in character array presenting position of the spinner
            _running(bool): maintains running state of the Spinner
        """

        # Can pick more from
        # https://stackoverflow.com/questions/2685435/cooler-ascii-spinners
        ASCII_CHAR = ['⣾', '⣽', '⣻', '⢿', '⡿', '⣟', '⣯', '⣷']

        def __init__(self, message):
            self._message: str = message[:70]
            self._lock = threading.Lock()
            self._position: int = 0
            self._running = True

            # starts the threat which survives till the spinner is running
            threading.Thread(target=self._step, daemon=True).start()

        def _step(self):
            """Continuous thread which updates the suffix character till _running is true"""
            while self._running:
                time.sleep(0.1)
                with self._lock:
                    self._position = (self._position + 1) % len(self.ASCII_CHAR)

        def done(self):
            """Stopping the Spinner"""
            self._running = False

        @property
        def message(self) -> str:
            return self._message

        def __str__(self) -> str:
            """Return str description of Spinner"""
            with self._lock:
                return self._message + ' ' + self.ASCII_CHAR[self._position]

    class __ProgressBar:
        """ Internal Class for Progressbar
        Progress bar is in form of
            Progress Bar Demo[########################################] (100/100)9.88it/s
                <label>                         <bar>                 (<Value>/<Total>) <rate><itr_format>

        Attributes:
              _label(str): The string printed before the bar, will be trimmed to maximum of 20 characters
              _value(int): current value of progress bar
              _max(int): maximum value of the progress bar
              _itr_label(str): the suffix for rate count, trimmed in 6 characters
              _state: current state of the progress bar
              _time: used for calculating the progres rate, initialised with progress bar is created
              _scale_factor: factor for the rate calculation - K, M, G. if None, it auto-scales.
              _bar_width: width of the progress bar - minimum 10 characters, it the length is too much,
                        it is likely to run across the screen.
        """

        RUNNING = 1
        PAUSED = 2
        STOPPED = 3

        def __init__(self, label: str, itr_label: str = 'it/s', bar_width: int = 40, scale_factor=Optional[str],
                     maxvalue: int = 100, fmt: str = ''):
            """Initializes the instance of Progres bar
            Args:
                label(str): the label to be printed as per bar format
                itr_label(str): the suffix for the rate, may be prefixed with scale factor
                bar_width(int): the width of the bar portion only, [...] for example are not included in this sizing
                scale_factor(str): option between None (autoscale), 'K', 'M' & 'G' and scales the rate accordingly.
            """

            self._label = label[:20]
            self._value = 0

            if not maxvalue:
                maxvalue = 100
            self._max = maxvalue

            self._itr_label = itr_label[:6]
            self._state = self.RUNNING

            self._time = time.time_ns()

            if scale_factor not in [None, 'K', 'M', 'G']:
                scale_factor = None
            self._scale_factor = scale_factor

            if bar_width < 10:
                bar_width = 10
            elif bar_width > 40:
                bar_width = 40
            self._bar_width = bar_width

            if not fmt:
                self._fmt = '{percentage:3.0f}%[{bar}]{value}/{total} : {rate} - {label}'

        def __str__(self) -> str:
            """Returns the string representative the current state of the progress bar"""
            percentage = (self._value / self._max) * 100
            bar_completed = floor((self._value / self._max) * self._bar_width)
            bar_remaining = self._bar_width - bar_completed
            bar = '#' * bar_completed + '-' * bar_remaining

            delta = int(time.time_ns() - self._time)
            # Avoid Div by Zero
            if not delta:
                delta = 1
            rate = (self._value / delta) * 10e8

            # auto-scale
            factor = 1
            scale_factor = ''

            # if None, Autoscale
            if self._scale_factor is None:
                if rate > 10e3 * 2:
                    scale_factor = 'K'
                if rate > 10e6 * 2:
                    scale_factor = 'M'
                if rate > 10e9 * 2:
                    scale_factor = 'G'
            else:
                scale_factor = self._scale_factor

            if scale_factor == 'K':
                factor = 10e3
            elif scale_factor == 'M':
                factor = 10e6
            elif scale_factor == 'G':
                factor = 10e9

            rate = round(rate / factor, 2)
            rate_str = str(rate) + scale_factor + self._itr_label

            # default '{percentage:3.0f}%[{bar}]{value}/{total} : {rate} - {label}'
            progress_string = self._fmt.format(percentage=percentage, bar=bar, value=self._value,
                                               total=self._max, label=self._label, rate=rate_str)

            return progress_string

        def step(self, value=1):
            """Increase the value count by specified number, default being 1"""
            # don't react on stopped
            if self._state != self.RUNNING:
                return

            self._value += value
            if self._value >= self._max:
                self._value = self._max
                self._state = self.STOPPED

        def set_max(self, value: int = 100):
            """Set/reset max value of bar"""
            if not value:
                value = 100
            self._max = value

        def label(self, message: str):
            """Set the progress bar label"""
            self._label = message.strip()

        def close(self):
            """To close actions on progress bar """
            self._state = self.STOPPED

        def reset(self):
            """Resets the timer for rate calculation"""
            self._value = 0
            self._time = time.time_ns()

    class __Commands:
        """ Internal class related to command(s)
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
            """Registers command - command_name invokes function"""
            if command_name.strip() == '':
                self.tui.ERROR('Registering Empty Command')
                return
            elif command_name in self._registered:
                self.tui.INFO(f'Registering duplicate command {command_name}, Ignored')
                return
            else:
                self._registered[command_name] = (function, tooltip)

        def get_command(self, command_name: str):
            """Returns corresponding function registered against given command_name"""
            command_name = command_name.strip()
            if not command_name:
                return None

            if command_name not in self._registered:
                return None

            return self._registered[command_name][0]

        def get_hints(self) -> []:
            """gets commands and respective hints"""
            hints = [(command_name, self._registered[command_name][1]) for command_name in self._registered]
            return hints

        def inc_cursor(self):
            """Increment cursor position"""
            self._cursor = self._cursor + 1

        def dec_cursor(self):
            """Decrement cursor position"""
            if self._cursor > 0:
                self._cursor = self._cursor - 1

        def reset_cursor(self):
            """Reset cursor position to 0"""
            self._cursor = 0

        @property
        def cursor(self) -> int:
            """return cursor position"""
            return self._cursor

        def set_mask_mode(self):
            """Set Mode to masked"""
            self._mode = self.CMD_MODE_PASSWORD

        def reset_mask_mode(self):
            """Reset mode to normal"""
            self._mode = self.CMD_MODE_NORMAL

        def is_masked(self):
            """Return mask state"""
            return self._mode

        @property
        def history(self) -> []:
            """Return list of commands"""
            return self._history

        def add_history(self, command: str):
            """Add command to history"""
            command = command.strip()
            if command:
                self._history.append(command)

    BOX_WIDTH = 1

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
        self._footer = None

        # Commands
        self._cmd = self.__Commands(self)
        self._cmd.reset_mask_mode()
        self._cmd.reset_cursor()

        self._prompt_lock = threading.Lock()
        self._shell_lock = threading.Lock()
        self._print_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._widget_lock = threading.Lock()

        # setting up dispatch queue for handling keystrokes
        self._dispatch_queue = queue.LifoQueue()
        self._input_queue = queue.LifoQueue()

        # For running list of widget
        self._widget = {}

        # let's set up the curses default window
        self._stdscr = curses.initscr()
        self._is_setup = False

        self._setup()

        # set minimum to 80x25 screen, if lesser better to exit than print weird or bad calculations
        if curses.COLS < 80 or curses.LINES < 25:
            self._shutdown()
            return

        # Set footer bar size, typically its one for tabs, one for prompt, one for application info,
        # and one each side for the box
        self._footer_height = 5

        self._resolution = {}
        self._resizeScreen()

        # Create windows - footer and tabs
        self._create_windows()

        # set the default tab
        self._activate('console')

        # Validation
        try:
            assert self._footer_height >= 5, 'TUI: Malformed Footer Size'
            assert self._footer is not None, 'TUI: Footer not defined'
            assert len([tab for tab in self._tabs if tab in ['console', 'log']]) == 2, 'TUI: Mandatory tabs missing'
            assert len([self._tabs[tab] for tab in self._tabs if self._tabs[tab]['selected']]) == 1, \
                'TUI: Tab not activated correctly'
        except AssertionError as e:
            self._shutdown()
            print(f'TUI: Setup configuration is wrong: {e}')
            return

        self.INFO("Initialising TUI environment")
        self._refresh()

        # Register the signal handler for SIGINT (Ctrl+C)
        signal.signal(signal.SIGINT, self._shutdown)

        self._cmd.register_command('clear', self.clear, 'Clears console tab, alternative tab name/all can be specified')
        self._cmd.register_command('demo', self.demo, 'Demonstrates inbuilt widgets & functions of TUI')
        self._cmd.register_command('history', self.history, 'Lists all commands executed')
        self._cmd.register_command('info', self.info, 'Prints system information')
        self._cmd.register_command('help', self.help, 'Prints registered command list and hints')

        # start the command
        threading.Thread(target=self.shell, daemon=True).start()

    def _refreshfooter(self):
        """_refreshfooter() - prints the footer section on each call"""

        tab_tooltip = "Use Tab key to rotate through Tabs"
        tab_prefix = 'Tabs:'
        tab_psutil = self._psutil.value

        # print tab list & tooltip
        self._footer.erase()
        self._footer.bkgd(curses.color_pair(self.COLOR_FOOTER))
        self._footer.box()
        self._footer.addstr(2, self._resolution['x'] - len(tab_tooltip) - self.BOX_WIDTH, tab_tooltip)
        self._footer.addstr(3, self._resolution['x'] - len(tab_psutil) - self.BOX_WIDTH, tab_psutil, curses.A_BOLD)

        self._footer.addstr(3, self.BOX_WIDTH, self._banner, curses.A_BOLD)

        # Displaying scrollable commandline
        # Usable space = screen width - 2 * BOX_WIDTH
        # Layout < [Box Width] [PROMPT] [One Spaces] [CMD] >
        # Less command prompt should be trimmed to 50% of the available space
        # Remain Command should be scrollable, cursor position defines from where the command is printed

        width = self._resolution['x'] - 2 * self.BOX_WIDTH

        # Trim to maximum length of 50% width
        cmd_prompt = self.CMD_PROMPT[:floor(width / 2)]

        # Available width - less length of cmd prompt and two (one for seperator, and another cursor)
        width = width - len(cmd_prompt) - 2

        # build cmd
        if not self._cmd.is_masked():
            cmd = self._cmd.current
        else:  # CMD_MODE_PASSWORD
            cmd = '*' * len(self._cmd.current)

        # Select portion of string relative to cursor position
        cmd = cmd[self._cmd.cursor:width + self._cmd.cursor]

        # Print <prompt><space><strip of cmd><blinking underscore as prompt>
        command_string = ''.join([cmd_prompt, ' ', cmd])
        self._footer.addstr(1, self.BOX_WIDTH, command_string)
        self._footer.addstr('_', curses.A_BLINK | curses.A_BOLD)
        self._footer.addstr(2, self.BOX_WIDTH, tab_prefix)
        index = self.BOX_WIDTH + len(tab_prefix)

        for tab in self._tabs:
            label = ''.join([' | ', tab, ' | '])
            if self._tabs[tab]['selected']:
                self._footer.addstr(2, index, label, curses.A_REVERSE)
            else:
                self._footer.addstr(2, index, label)
            index += len(label)

        self._footer.refresh()

    def _refreshtab(self):
        """_refreshtab() - Refresh/Re-paint the active tab"""

        # printing the Active tab only
        active_tab = self._activetab

        # logic implements scrolling find minimum of curser and number of lines in buffer,
        # to avoid it scrolling past the buffer with lines less than screen height
        buffer = active_tab['buffer']
        window = active_tab['win']
        cursor = min(active_tab['cursor'], len(buffer))
        window.erase()
        for i in range(cursor):
            line = buffer[i]
            window.addstr(line[0], line[1])

        with self._widget_lock:
            for widget in self._widget:
                line = ''.join([str(self._widget[widget]), '\n'])
                window.addstr(line, curses.A_BOLD)

        window.refresh()

    def _refresh(self, force=False):
        """_refresh() - refreshes all windows
        Args:
            force(bool): forces complete screen refresh
        """
        if curses.is_term_resized(self._resolution['y'], self._resolution['x']):
            self._shutdown()
            self._setup()
            self._resizeScreen()
            self._create_windows()
            force = True

        if force:
            self._stdscr.touchwin()

        with self._refresh_lock:
            self._refreshfooter()
            self._refreshtab()

            # not sure if this is needed, both _refreshtab & _refreshfooter calls individual window.refresh()
            self._stdscr.refresh()

    @property
    def _activetab(self) -> {}:
        """_activetab - returns tab marked as 'selected'"""
        active_tab = [self._tabs[tab] for tab in self._tabs if self._tabs[tab]['selected']]
        if len(active_tab) != 1:
            self.ERROR('TUI: Active State for tabs inconsistent')
        active_tab = active_tab[0]
        return active_tab

    def _res_util(self):
        """_res_util - maintains the _psutil.value string giving the resource utilisation, will run as infinite loop """
        while True:
            # Get CPU usage as a percentage - currently on static interval of 2 secs
            cpu_percent = psutil.cpu_percent(interval=2)

            # Get memory usage statistics
            mem = psutil.virtual_memory()
            mem_percent = mem.percent

            # Get disk usage statistics
            disk = psutil.disk_usage('/')
            disk_percent = disk.percent

            # String for results, operation is atomic internally
            self._psutil.value = f'CPU: {cpu_percent}% RAM: {mem_percent}% Disk(/): {disk_percent}%'

    def _activate(self, name):
        """_activate - mark 'name' as active tab"""
        if name not in self._tabs:
            self.ERROR('TUI: Activating non available Tab')
            return

        for tab in self._tabs:
            self._tabs[tab]['selected'] = False

        self._tabs[name]['selected'] = True

        # forces refresh to repaint tab and footer
        self._refresh()

    def _resizeScreen(self):
        """_resizeScreen - recalculate layout (width, height, origin y, origin x) with origin on top left corner"""
        curses.update_lines_cols()
        self._resolution = {'x': curses.COLS, 'y': curses.LINES}
        self._tab_coordinates = {'h': self._resolution['y'] - self._footer_height, 'w': self._resolution['x'],
                                 'y': 0, 'x': 0}
        self._footer_coordinates = {'h': self._footer_height, 'w': self._resolution['x'],
                                    'y': self._resolution['y'] - self._footer_height, 'x': 0}

    def _log(self, severity, message):
        """_log - the parent function to add text to log tab, severity determines the attribute"""

        assert (severity in [self.SEVERITY_ERROR, self.SEVERITY_WARNING, self.SEVERITY_INFO]), \
            f'TUI: Incorrect Severity {severity} defined'

        with self._log_lock:
            attribute = curses.color_pair(self.COLOR_NORMAL)
            if severity == self.SEVERITY_ERROR:
                attribute = curses.color_pair(self.COLOR_ERROR)
            elif severity == self.SEVERITY_WARNING:
                attribute = curses.color_pair(self.COLOR_WARNING)

            logger = self._tabs['log']
            logger['buffer'].append((message + '\n', attribute))
            logger['cursor'] = len(logger['buffer'])

    def _create_windows(self):
        """_build_windows - creates the _footer and _tab windows, discards old windows"""
        # empty previous window
        if not self._footer:
            self._footer = None

        # creating footer, Cant create tab before that
        self._footer = curses.newwin(self._footer_coordinates['h'], self._footer_coordinates['w'],
                                     self._footer_coordinates['y'], self._footer_coordinates['x'])

        if self._tabs:
            self._tabs.clear()

        for tab_name in ['console', 'log']:
            name = tab_name.strip()

            # Should not already exist
            if not name or name in self._tabs:
                continue

            # Tab is a tuple of name, window, buffer, cursor position, and selected state
            self._tabs[name] = {'win': curses.newwin(self._tab_coordinates['h'], self._tab_coordinates['w'],
                                                     self._tab_coordinates['y'], self._tab_coordinates['x']),
                                'buffer': [], 'cursor': 0, 'selected': True}

            # Enabling Scrolling
            self._tabs[name]['win'].scrollok(True)

    def _shutdown(self):
        """_shutdown - shuts down the curses environment"""
        # if not previously setup - skip
        if not self._is_setup:
            return

        self._is_setup = False

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

    def _setup(self):
        """_setup - initialises the curses environment"""

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
        curses.init_pair(self.COLOR_NORMAL, self.fgColor, self.bgColor)
        curses.init_pair(self.COLOR_REVERSE, self.bgColor, self.fgColor)
        curses.init_pair(self.COLOR_WARNING, self.warningColor, self.bgColor)
        curses.init_pair(self.COLOR_ERROR, self.errorColor, self.bgColor)
        curses.init_pair(self.COLOR_HIGHLIGHT, self.highlightColor, self.bgColor)
        curses.init_pair(self.COLOR_FOOTER, self.fgColor, self.bgFooter)

        # no waiting on getch()
        self._stdscr.nodelay(True)

        # END ncurses startup/initialization...
        self._is_setup = True

    def enabletab(self, name):
        """enableTab - activate the given tab name. this is public function"""
        # Set current Tab based on name, provided it is valid
        if name in self._tabs:
            self._activate(name)

    def _enable_next_tab(self):
        """enable_next_tab: Finds and enables next tab, rotates to first if we reach the end"""
        active_tab = [tab for tab in self._tabs if self._tabs[tab]['selected']]
        if len(active_tab) != 1:
            self.ERROR('TUI: More than one tab active')

        tab_list = list(self._tabs)

        # select next tab on the dict
        try:
            next_tab = tab_list[tab_list.index(active_tab[0]) + 1]
        except (ValueError, IndexError):
            next_tab = tab_list[0]

        self._activate(next_tab)

    def run(self):
        """run - The main thread which is accepting and dispatches keys"""
        if not self._is_setup:
            print("TUI: screen has not been setup properly")
            return

        # maintaining internal flag to exit loop on exit
        __quit = False

        # main loop
        while not __quit:
            # wait 1ms to avoid 100% CPU usage
            curses.napms(10)

            self._refresh()

            # get input
            try:
                c = self._stdscr.getkey()
            except curses.error:
                c = None

            if c is not None:

                activetab = self._activetab

                # KEY_UP & KEY_DOWN are only for scrolling, cannot be passed to command
                # If screen content < screen size - do not scroll
                if c == 'KEY_UP':
                    if activetab['cursor'] > self._tab_coordinates['h']:
                        activetab['cursor'] -= 1
                        continue
                elif c == 'KEY_DOWN':
                    activetab['cursor'] = min(len(activetab['buffer']), activetab['cursor'] + 1)
                    continue

                # KEY_RIGHT & KEY_LEFT are only for scrolling commandline
                elif c == 'KEY_RIGHT':
                    width = self._resolution['x'] - 2 * self.BOX_WIDTH
                    width = width - len(self.CMD_PROMPT[:floor(width / 2)]) - 2
                    # Only if the input is greater than the available space is cursor position relevant
                    if len(self._cmd.current) > width:
                        if self._cmd.cursor < len(self._cmd.current) - width:
                            self._cmd.inc_cursor()
                elif c == 'KEY_LEFT':
                    self._cmd.dec_cursor()

                # handler for other keys
                elif c == 'KEY_BACKSPACE':
                    self._cmd.current = self._cmd.current[:-1]
                    if self._cmd.cursor > 0:
                        self._cmd.dec_cursor()
                    continue

                # Tab key circles through available tabs
                elif c == '\t':
                    # switch to next 'Tab' on Alt
                    self._enable_next_tab()
                    continue

                # Simple hack - if it is longer than a char it is a special
                # key string that we care not currently handling
                if len(c) > 1:
                    continue

                # here onwards we are processing keys outside control keys
                # if nothing is waiting in queue don't process any keys
                if self._dispatch_queue.empty():
                    continue

                # Newline received, based on data input mode the dispatch sequence is identified
                if c == '\n':
                    # Command has been completed
                    if self._cmd.current.strip():
                        # Special Case
                        if self._cmd.current.strip() in ['quit', 'exit', 'q']:
                            __quit = True
                            continue
                        else:
                            self._input_queue.put(self._cmd.current)
                            self._cmd.add_history(self._cmd.current)
                            try:
                                condition = self._dispatch_queue.get()
                            except queue.Empty:
                                self.ERROR('TUI: Nothing in dispatch queue')
                                continue

                            with condition:
                                condition.notify()

                    self._cmd.current = ''
                    self._cmd.reset_cursor()

                # if we are here, c is a valid part of the command being typed, append to it and increment the cursor
                else:
                    self._cmd.current = ''.join([self._cmd.current, c])
                    width = self._resolution['x'] - 2 * self.BOX_WIDTH
                    width = width - len(self.CMD_PROMPT[:floor(width / 2)]) - 2
                    # Only if the input is greater than the available space is cursor position relevant
                    if len(self._cmd.current) > width:
                        if self._cmd.cursor < len(self._cmd.current) - width:
                            self._cmd.inc_cursor()

        # broken out of the loop - clean up
        self._shutdown()

    def INFO(self, message):
        """INFO - Prints an info severity log"""
        self._log(self.SEVERITY_INFO, message)

    def WARNING(self, message):
        """INFO - Prints a warning severity log"""
        self._log(self.SEVERITY_WARNING, message)

    def ERROR(self, message):
        """INFO - Prints an error severity log"""
        self._log(self.SEVERITY_ERROR, message)

    def print(self, message, attribute=None):
        """print - prints text to console tab, this will replace the typical use of python print in code
        Args:
            message(str): The message to print, adds newline character on print
            attribute: the attribute for the text, uses COLOR_NORMAL as default
        """
        with self._print_lock:
            if attribute is None:
                attribute = curses.color_pair(self.COLOR_NORMAL)
            console = self._tabs['console']

            console['buffer'].append((''.join([message, '\n']), attribute))
            console['cursor'] = len(console['buffer'])

    def clear(self, name):
        """clear - Clear the named tab
        Args:
            name(str): the 'tab' to clear, all specifies all tabs
        """

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
        """history - prints list of previous commands"""
        # The last command will be 'history' - hence skipped
        for cmd in self._cmd.history[:-1]:
            self.print(cmd)

    def shell(self):
        """shell - Run as a separate thread which executes command from dispatch queue"""
        with self._shell_lock:

            while True:
                self.CMD_PROMPT = '$'

                # Basic approach is to push request in waiting queue,
                # and when condition is called execute the command in the _input_queue
                condition = threading.Condition()
                self._dispatch_queue.put(condition)

                with condition:
                    condition.wait()

                try:
                    command = self._input_queue.get()
                    command = command.strip()
                except queue.Empty:
                    self.ERROR('TUI: Condition called but nothing in Input Stack')
                    continue

                self.print(command, curses.color_pair(self.COLOR_HIGHLIGHT))

                # Do not accept commands on prompt till command is competed
                self.CMD_PROMPT = '(command under progress)'
                self.INFO(f'Executing "{command}"')

                cmd_parts = command.split()
                if len(cmd_parts) > 0:
                    function_name = cmd_parts[0]
                    function_args = cmd_parts[1:]

                    function = self._cmd.get_command(function_name)

                    if not function:
                        self.print(f'Command {function_name} not found')
                        continue

                    try:
                        function(*function_args)
                    except TypeError as e:
                        self.print(f"Error: {e}")
                        self.ERROR(f"Error: {e}")

    def prompt(self, prompt_type, message, options=None) -> str:
        """prompt - gets user input and returns as string
        Args:
            prompt_type: defines how the prompt is shown
            message(str): the string printed while waiting for user input
            options([] | None): defines the set of strings to choose from if the prompt type is PROMPT_OPTIONS
        Returns:
            string giving out the user input
        """
        if options is None:
            options = []

        # Confirm valid prompt type
        if prompt_type not in [self.PROMPT_YESNO, self.PROMPT_OPTIONS, self.PROMPT_PASSWORD, self.PROMPT_INPUT]:
            self.ERROR('TUI: Unknown prompt type given')
            return ""

        # Cannot call PROMPT_OPTIONS with no Options
        if prompt_type == self.PROMPT_OPTIONS and len(options) > 0:
            self.ERROR('Prompt type PROMPT_OPTIONS called without options')
            return ""

        # same technique as others
        #   - put condition on dispatch queue
        #   - wait for condition to be notified
        #   - get input from queue, confirm if suitable e.g. Option/ YesNo

        with self._prompt_lock:
            old_prompt = self.CMD_PROMPT
            self.CMD_PROMPT = message

            if prompt_type == self.PROMPT_YESNO:
                self.CMD_PROMPT += ' (y/n):'
            elif prompt_type == self.PROMPT_OPTIONS:
                self.CMD_PROMPT += str(options)

            answer = ''
            while True:
                condition = threading.Condition()
                self._dispatch_queue.put(condition)

                # mask mode is set for PROMPT_PASSWORD and reset when input if received
                if prompt_type == self.PROMPT_PASSWORD:
                    self._cmd.set_mask_mode()

                with condition:
                    condition.wait()

                try:
                    answer = self._input_queue.get()
                except queue.Empty:
                    self.ERROR('TUI: Condition called but nothing in Input Stack')

                if prompt_type == self.PROMPT_OPTIONS and answer not in options:
                    self.print('TUI Prompt: Only answers within the option provided are permitted')
                    continue

                if prompt_type == self.PROMPT_YESNO and answer not in ['y', 'Y', 'n', 'N', 'yes', 'Yes', 'no', 'No']:
                    self.print('TUI Prompt: Only answers related to yes/no are permitted')
                    continue

                break

            # for PROMPT_PASSWORD print masked content of same length else clear text
            if prompt_type == self.PROMPT_PASSWORD:
                self._cmd.reset_mask_mode()
                self.print(self.CMD_PROMPT + ' ' + '*' * len(answer))
            else:
                self.print(self.CMD_PROMPT + ' ' + answer)
            self.CMD_PROMPT = old_prompt

        return answer

    def spinner(self, message) -> __Spinner:
        """spinner - return instance of __Spinner
        Args:
            message(str): The string to be printed before the spinner
        Returns:
              instance of __Spinner
        """
        spin = Tui.__Spinner(message)
        widget_id = spin.__hash__()
        # add it to _widget to render
        with self._widget_lock:
            self._widget[widget_id] = spin
        return spin

    def s_stop(self, spin: __Spinner):
        """s_stop - stops spinner and removes from _widget list
        Args:
            spin(__Spinner): instance to stop
        """
        spin.done()
        widget_id = spin.__hash__()
        # print completion and remove from _widget list
        with self._widget_lock:
            if widget_id not in self._widget:
                self.print(f'TUI: No Widget by id {widget_id}')
                return
            self.print(spin.message + '... Done')
            self._widget.pop(widget_id)

    def progressbar(self, label: str, itr_label='it/s', bar_width: int = 40, scale_factor: str = Optional[str],
                    maxvalue: int = 100, fmt: str = '', ) -> __ProgressBar:
        """
        Creates instance of progressbar and adds to _widget list to render, all actions are on the instance
        Args:
            label(str): the label to be printed as per bar format
            itr_label(str): the suffix for the rate, may be prefixed with scale factor
            bar_width(int): the width of the bar portion only, [...] for example are not included in this sizing
            scale_factor(str): option between None (autoscale), 'K', 'M' & 'G' and scales the rate accordingly.
            maxvalue(int): Maximum value of progressbar, bar is always created with value zero though
            fmt: Format string for progressbar layout,
                    default is '{percentage:3.0f}%[{bar}]{value}/{total} : {rate} - {label}'
        Returns:
            instance of progressbar
        """
        bar = Tui.__ProgressBar(label, itr_label, bar_width, scale_factor, maxvalue, fmt)

        widget_id = bar.__hash__()
        with self._widget_lock:
            self._widget[widget_id] = bar

        return bar

    def p_close(self, bar: __ProgressBar):
        """p_close - mark progressbar as completed and remove from _widget list
        Args:
            bar(__ProgressBar): instance to close
        """
        bar.close()
        widget_id = bar.__hash__()
        with self._widget_lock:
            if widget_id not in self._widget:
                self.print(f'TUI: No Widget by id {widget_id}')
                return
            self.print(str(self._widget[widget_id]))
            self._widget.pop(widget_id)

    def info(self):
        """info - prints basic system information"""
        import platform
        os_name = platform.system()
        os_version = platform.release()

        import os
        cwd = os.getcwd()

        import socket
        hostname = socket.gethostname()

        util_str = self._psutil.value

        self.print(f'Athena Version: {self._banner}')
        self.print(f'OS: {os_name}')
        self.print(f'Version: {os_version}')
        self.print(f'Hostname: {hostname}')
        self.print(f'Current Directory: {cwd}')
        self.print(f'Resource: {util_str}')
        self.print(f"Screen Resolution: {self._resolution['y']}x{self._resolution['x']}")

    def demo(self):
        """demo - demonstrated basic prompt, spinner and progressbar functionalities"""
        spin = self.spinner('Starting Demo')
        self.prompt(self.PROMPT_YESNO, 'This is YES NO prompt')
        self.prompt(self.PROMPT_INPUT, 'This accepts Input string')
        self.prompt(self.PROMPT_OPTIONS, 'This allows you to select from options', ['yes', 'no'])
        self.prompt(self.PROMPT_PASSWORD, 'This accepts masked input')

        bar = self.progressbar('Progress Bar Demo', maxvalue=100)

        for i in range(100):
            bar.step(value=1)
            curses.napms(100)
        self.p_close(bar)

        self.s_stop(spin)

    def help(self):
        """help - prints the registered commands and hints"""
        for command in self._cmd.get_hints():
            self.print(f'{command[0]}\t-\t{command[1]}')


# test function - can run this file separately 
if __name__ == '__main__':
    tui = Tui("Athena Build Environment v0.1")
    tui.run()
