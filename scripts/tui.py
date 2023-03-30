#  Copyright (c) 2023. Harkirat S Virk <harkiratsvirk@gmail.com>
#
#  This program comes with ABSOLUTELY NO WARRANTY; for details see COPYING.
#  This is free software, and you are welcome to redistribute it under certain conditions; see COPYING for details.

import curses
import signal
import time
from math import floor
from time import sleep

import psutil
import queue
import threading


class Lockable:
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


class Tui:
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
    CMD_MODE_PASSWORD = 1

    bgColor = curses.COLOR_BLACK
    bgFooter = curses.COLOR_BLUE
    fgColor = curses.COLOR_WHITE
    warningColor = curses.COLOR_YELLOW
    errorColor = curses.COLOR_RED
    highlightColor = curses.COLOR_GREEN

    CMD_PROMPT = '$ '

    def __init__(self, banner: str):

        # Banner String, needs to be trimmed
        banner_trim = 30
        self.__banner = banner[:banner_trim]

        # Set up the for running the __res_util as a parallel thread
        self.__psutil = Lockable(str)
        threading.Thread(target=self.__res_util, daemon=True).start()

        # collection of tabs - tuple of name, window, buffer, cursor position
        self.__tabs = {}

        # footer defined separately
        self.__footer = None

        # Commands
        self.__cmd_current = ''
        self.__cmd_history = []
        self.__registered_cmd = {}
        self.__cmd_mode = self.CMD_MODE_NORMAL
        self.__prompt_str = ''

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
        self.stdscr = curses.initscr()

        self.__setup__()

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
        assert len([__tab for __tab in self.__tabs if __tab in ['console', 'log']]) == 2, 'TUI: Mandatory tabs missing'
        assert len([self.__tabs[tab] for tab in self.__tabs if self.__tabs[tab]['selected']]) == 1, \
            'TUI: Tab not activated correctly'

        self.__log__(self.SEVERITY_INFO, "Initialising TUI environment")
        self.__refresh__()
        self.stdscr.nodelay(True)
        self.stdscr.refresh()

        # Register the signal handler for SIGINT (Ctrl+C)
        signal.signal(signal.SIGINT, self.__shutdown__)

        self.register_command('clear', self.clear)
        self.register_command('wait', self.wait)
        self.register_command('demo', self.demo)

        # start the command
        threading.Thread(target=self.shell, daemon=True).start()

    def __refreshfooter__(self):
        tab_tooltip = "Use Tab to rotate through Tabs"
        tab_prefix = 'Tabs:'
        tab_psutil = self.__psutil.value

        # print tab list & tooltip
        self.__footer.erase()
        self.__footer.bkgd(curses.color_pair(self.COLOR_FOOTER))
        self.__footer.box()
        self.__footer.addstr(2, self.__resolution['x'] - len(tab_tooltip) - self.BOX_WIDTH, tab_tooltip)
        self.__footer.addstr(3, self.__resolution['x'] - len(tab_psutil) - self.BOX_WIDTH, tab_psutil, curses.A_BOLD)

        self.__footer.addstr(3, self.BOX_WIDTH, self.__banner, curses.A_BOLD)

        assert self.__cmd_mode in [self.CMD_MODE_NORMAL, self.CMD_MODE_PASSWORD], \
            'TUI: Incorrect __current_mode defined'

        if self.__cmd_mode == self.CMD_MODE_NORMAL:
            self.__footer.addstr(1, self.BOX_WIDTH, self.CMD_PROMPT + ' ' + self.__cmd_current)
        else:  # CMD_MODE_PASSWORD
            self.__footer.addstr(1, self.BOX_WIDTH, self.CMD_PROMPT + ' ' + '*' * len(self.__cmd_current))

        # we should have written till
        self.__footer.addstr(2, self.BOX_WIDTH, tab_prefix)
        index = self.BOX_WIDTH + len(tab_prefix)

        for tab in self.__tabs:
            label = ' | ' + tab + ' | '
            if self.__tabs[tab]['selected']:
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
                line = self.__widget[widget]
                window.addstr(line[0], line[1])

        window.refresh()

    def __refresh__(self, force=False):
        with self.__refresh_lock:
            self.__refreshfooter__()
            self.__refreshtab__()
            self.stdscr.refresh()

    @property
    def __activetab__(self) -> {}:
        active_tab = [self.__tabs[tab] for tab in self.__tabs if self.__tabs[tab]['selected']]
        assert len(active_tab) == 1, 'TUI: Active State for tabs inconsistent'
        active_tab = active_tab[0]
        return active_tab

    def __res_util(self):
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
            self.__psutil.value = f'CPU: {cpu_percent}% RAM: {mem_percent}% Disk(/): {disk_percent}%'

    def __activate__(self, name):
        assert name in self.__tabs, 'TUI: Activating non available Tab'

        for tab in self.__tabs:
            self.__tabs[tab]['selected'] = False

        self.__tabs[name]['selected'] = True
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

            logger = self.__tabs['log']
            logger['buffer'].append((message + '\n', attribute))
            logger['cursor'] = len(logger['buffer'])

    def __shutdown__(self):
        # BEGIN ncurses shutdown/de-initialization...
        # Turn off cbreak mode...
        curses.nocbreak()

        # Turn echo back on.
        curses.echo()

        # Restore cursor blinking.
        curses.curs_set(True)

        # Turn off the keypad...
        self.stdscr.keypad(False)

        # Restore Terminal to original state.
        curses.endwin()

        # END ncurses shutdown/de-initialization...

    def __setup__(self):
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
        self.stdscr.keypad(True)

        # Set color pairs
        # TODO: load from tui.conf else use defaults

        curses.init_pair(self.COLOR_NORMAL, self.fgColor, self.bgColor)
        curses.init_pair(self.COLOR_REVERSE, self.bgColor, self.fgColor)
        curses.init_pair(self.COLOR_WARNING, self.warningColor, self.bgColor)
        curses.init_pair(self.COLOR_ERROR, self.errorColor, self.bgColor)
        curses.init_pair(self.COLOR_HIGHLIGHT, self.highlightColor, self.bgColor)
        curses.init_pair(self.COLOR_FOOTER, self.fgColor, self.bgFooter)

        # END ncurses startup/initialization...

    def addtab(self, name: str):
        # Strip whitespaces
        name = name.strip()

        # Should not already exist
        assert name not in self.__tabs, f'TUI: Attempted creating tab with name "{name}" which already exists'

        if name != '':
            # Tab is a tuple of name, window, buffer, cursor position, and selected state
            self.__tabs[name] = {'win': curses.newwin(self.__tab_coordinates['h'], self.__tab_coordinates['w'],
                                                      self.__tab_coordinates['y'], self.__tab_coordinates['x']),
                                 'buffer': [], 'cursor': 0, 'selected': True}
            # #nabling Scrolling
            self.__tabs[name]['win'].scrollok(True)

    def enabletab(self, name):
        # Set current Tab based on name, provided it is valid
        if name in self.__tabs:
            self.__activate__(name)

    def enable_next_tab(self):
        # dict are not sorted, so there is no order.
        active_tab = [tab for tab in self.__tabs if self.__tabs[tab]['selected']]
        assert len(active_tab) == 1, 'TUI: More than one tab active'
        tab_list = list(self.__tabs)
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
                c = self.stdscr.getkey()
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
                elif c == 'KEY_BACKSPACE':
                    self.__cmd_current = self.__cmd_current[:-1]
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
                    if not self.__cmd_current.strip() == '':
                        # Special Case
                        if self.__cmd_current.strip() in ['quit', 'exit', 'q']:
                            __quit = True
                            continue
                        else:
                            self.__input_queue.put(self.__cmd_current)
                            self.__cmd_history.append(self.__cmd_current)
                            try:
                                condition = self.__dispatch_queue.get()
                            except queue.Empty:
                                self.ERROR('TUI: Nothing in dispatch queue')
                                continue

                            with condition:
                                condition.notify()

                    self.__cmd_current = ''

                else:
                    self.__cmd_current += c

            else:
                curses.napms(10)  # wait 10ms to avoid 100% CPU usage

        # clean up
        self.__shutdown__()

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
            console = self.__tabs['console']

            console['buffer'].append((message + '\n', attribute))
            console['cursor'] = len(console['buffer'])

    def clear(self, name):
        if name == 'all':
            for tab in self.__tabs:
                self.__tabs[tab]['buffer'] = []
                self.__tabs[tab]['cursor'] = 0
        elif name in self.__tabs:
            self.__tabs[name]['buffer'] = []
            self.__tabs[name]['cursor'] = 0
        else:
            self.print(f'Attempted to clear non-existent tab {name}')

    def register_command(self, command_name: str, function, tooltip=''):
        if command_name.strip() == '':
            self.ERROR('Registering Empty Command')
            return
        elif command_name in self.__registered_cmd:
            self.INFO(f'Registering duplicate command {command_name}, Ignored')
            return
        else:
            self.__registered_cmd[command_name] = (function, tooltip)

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

                    if function_name not in self.__registered_cmd:
                        self.print(f'Command {function_name} not found')
                        continue

                    function = self.__registered_cmd[function_name][0]
                    try:
                        function(*function_args)
                    except TypeError as e:
                        self.print(f"Error: {e}")
                        self.ERROR(f"Error: {e}")

    def prompt(self, prompt_type, message, options=[]) -> str:
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
                    self.__cmd_mode = self.CMD_MODE_PASSWORD

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
                self.__cmd_mode = self.CMD_MODE_NORMAL
                self.print(self.CMD_PROMPT + ' ' + '*' * len(answer))
            else:
                self.print(self.CMD_PROMPT + ' ' + answer)
            self.CMD_PROMPT = old_prompt

        return answer

    def spinner(self):
        class __ProgressBar:
            RUNNING = 1
            PAUSED = 2
            STOPPED = 3


            def __init__(self, message, itr_label='it/s', scale_factor='', ):
                self.__label = message[:20]
                self.__value = 0
                self.__max = 100
                self.__rate = 0
                self.itr_label = itr_label[:6]

                self.__time = time.time_ns()
                self.__state = self.RUNNING

                if scale_factor not in ['', 'K', 'M', 'G']:
                    scale_factor = ''

                self.scale_factor = scale_factor

            def __str__(self):
                completed = floor((self.__value / self.__max) * 100)
                remaining = 100 - completed
                string = self.__label + '[' + '#' * completed + '-' + '] ' + '(' + self.__value + '/' + self.__max + ')'
                return

            def step(self, value=1):
                # don't react on stopped
                if self.__state != self.RUNNING:
                    return

                self.__value += value
                if self.__value >= self.__max:
                    self.__value = self.__max

                self.__rate = (self.__value / (time.time_ns() - self.__time)) * (10 ^ -9)

            def max(self, value=100):
                if not value > 0:
                    value = 100

                self.__max = value
                self.step(0)

            def label(self, message):
                self.__label = message


        assert mode in [self.SPINNER_START, self.SPINNER_STOP], 'TUI: Incorrect Spinner mode given'

    def persistent(self):
        pass

    @staticmethod
    def wait(self, duration=1000):
        sleep(10)

    def demo(self):
        self.prompt(self.PROMPT_YESNO, 'Do You want to exit')
        self.prompt(self.PROMPT_INPUT, 'Do you want to exit?')
        self.prompt(self.PROMPT_OPTIONS, 'Do you want to exit?', ['yes', 'no'])
        self.prompt(self.PROMPT_PASSWORD, 'Enter Password to exit')


# Main function
if __name__ == '__main__':
    tui = Tui("Athena Build Environment v0.1")
    tui.run()
