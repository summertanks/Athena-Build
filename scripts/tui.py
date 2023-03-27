#  Copyright (c) 2023. Harkirat S Virk <harkiratsvirk@gmail.com>
#
#  This program comes with ABSOLUTELY NO WARRANTY; for details see COPYING.
#  This is free software, and you are welcome to redistribute it under certain conditions; see COPYING for details.

import curses
import signal

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

    CMD_MODE_DISABLE = 1
    CMD_MODE_NORMAL = 2
    CMD_MODE_PROMPT = 3
    CMD_MODE_PASSWORD = 4

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

        # setting up dispatch queue for handling keystrokes
        self.__dispatch_queue = queue.Queue

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

        assert self.__cmd_mode in \
               [self.CMD_MODE_PROMPT, self.CMD_MODE_NORMAL, self.CMD_MODE_DISABLE, self.CMD_MODE_PASSWORD], \
            'TUI: Incorrect __current_mode defined'

        if self.__cmd_mode == self.CMD_MODE_DISABLE:
            self.__footer.addstr(1, self.BOX_WIDTH, '(Command under Progress)', curses.A_ITALIC)
        elif self.__cmd_mode == self.CMD_MODE_NORMAL:
            self.__footer.addstr(1, self.BOX_WIDTH, self.CMD_PROMPT + self.__cmd_current)
        elif self.__cmd_mode == self.CMD_MODE_PROMPT:
            self.__footer.addstr(1, self.BOX_WIDTH, self.__prompt_str + self.__cmd_current)
        else:  # CMD_MODE_PASSWORD = 4
            self.__footer.addstr(1, self.BOX_WIDTH, self.__prompt_str + '*' * len(self.__cmd_current))

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
        window.refresh()

    def __refresh__(self, force=False):

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

    def __executecmd__(self, cmd):

        self.INFO(f'Executing "{cmd}"')
        cmd_parts = cmd.split()
        if len(cmd_parts) > 0:
            function_name = cmd_parts[0]
            function_args = cmd_parts[1:]
            if function_name not in self.__registered_cmd:
                self.print(f'Command {function_name} not found')
                return
            try:
                threading.Thread(target=self.__exec_thread__, args=(function_name, function_args)).start()
            except threading.ThreadError as e:
                self.print(f"Error: {e}")
                self.ERROR(f"Error: {e}")

    def __exec_thread__(self, function_name, function_args):
        self.__cmd_mode = self.CMD_MODE_DISABLE
        function = self.__registered_cmd[function_name][0]
        try:
            function(*function_args)
        except TypeError as e:
            self.print(f"Error: {e}")
        # self.__cmd_mode = self.CMD_MODE_NORMAL

    def __log__(self, severity, message):
        assert (severity in [self.SEVERITY_ERROR, self.SEVERITY_WARNING, self.SEVERITY_INFO]), \
            f'TUI: Incorrect Severity {severity} defined'

        attribute = curses.color_pair(self.COLOR_NORMAL)
        if severity == self.SEVERITY_ERROR:
            attribute = curses.color_pair(self.COLOR_ERROR)
        elif severity == self.SEVERITY_WARNING:
            attribute = curses.color_pair(self.COLOR_WARNING)

        logger = self.__tabs['log']
        logger['buffer'].append((message + '\n', attribute))
        logger['cursor'] = len(logger['buffer'])
        self.__refresh__()

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

    def __register_handler(self, function):
        pass

    def __deregister_handler(self, function):
        pass

    def __dispatch_key__(self, char):
        pass

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
        self.__refresh__()

        __quit = False
        # main loop
        while not __quit:
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
                        self.__refresh__()
                elif c == 'KEY_DOWN':
                    activetab['cursor'] = min(len(activetab['buffer']), activetab['cursor'] + 1)
                    self.__refresh__()
                elif c == 'KEY_BACKSPACE':
                    self.__cmd_current = self.__cmd_current[:-1]
                    self.__refresh__()
                elif c == '\t':
                    # switch to next Tab on Alt
                    self.enable_next_tab()
                    continue

                # Simple hack - if it is longer than a char it is a special key string
                elif len(c) > 1:
                    continue

                # Newline received, based on data input mode the dispatch sequence is identified
                elif c == '\n':
                    # Command has been completed
                    if not self.__cmd_current.strip() == '':
                        # Special Case
                        if self.__cmd_current.strip() in ['quit', 'exit', 'q']:
                            __quit = True
                            continue
                        else:
                            self.print(self.__cmd_current, curses.color_pair(self.COLOR_HIGHLIGHT))
                            self.__cmd_history.append(self.__cmd_current)
                            self.__executecmd__(self.__cmd_current)
                    self.__cmd_current = ''
                    self.__refresh__()

                else:
                    self.__cmd_current += c
                    self.__refresh__()
            else:
                curses.napms(10)  # wait 10ms to avoid 100% CPU usage
                self.__refreshfooter__()

        # clean up
        self.__shutdown__()

    def INFO(self, message):
        self.__log__(self.SEVERITY_INFO, message)

    def WARNING(self, message):
        self.__log__(self.SEVERITY_WARNING, message)

    def ERROR(self, message):
        self.__log__(self.SEVERITY_ERROR, message)

    def print(self, message, attribute=None):

        if attribute is None:
            attribute = curses.color_pair(self.COLOR_NORMAL)
        console = self.__tabs['console']

        console['buffer'].append((message + '\n', attribute))
        console['cursor'] = len(console['buffer'])
        self.__refresh__()

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

    @staticmethod
    def wait(self, duration=1000):
        curses.napms(duration)

    def register_command(self, command_name: str, function, tooltip=''):
        if command_name.strip() == '':
            self.ERROR('Registering Empty Command')
            return
        elif command_name in self.__registered_cmd:
            self.INFO(f'Registering duplicate command {command_name}, Ignored')
            return
        else:
            self.__registered_cmd[command_name] = (function, tooltip)

    def prompt(self, prompt_type, message, options) -> str:
        assert prompt_type in [self.PROMPT_YESNO, self.PROMPT_OPTIONS, self.PROMPT_PASSWORD, self.PROMPT_INPUT], \
            f'TUI: Unknown prompt type given'

        self.__cmd_current = message + ' (y/n)'
        self.stdscr.touchwin()
        return ''


# Main function
if __name__ == '__main__':
    tui = Tui("Athena Build Environment v0.1")
    tui.run()
