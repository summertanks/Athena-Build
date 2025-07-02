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
from curses.panel import panel
import curses.panel
import signal
import time

import psutil
import queue
import threading
from math import floor
from queue import LifoQueue
from typing import Optional, Any, Callable, Tuple, List, Dict, TypedDict
from types import FrameType


Print = None
Exit = None
Pause = None

class _TabEntry(TypedDict):
    win: curses.window
    panel: panel
    buffer: List[Tuple[str, int]]
    cursor: int
    selected: bool

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
        _quit(bool): internal flag to trigger TUI/Application Exit
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
        def __init__(self, var_type: Any):
            self.__lock = threading.Lock()
            self.__var = var_type()

        @property
        def value(self):
            with self.__lock:
                return self.__var

        @value.setter
        def value(self, var: Any):
            assert isinstance(self.__var, type(var)), 'Lockable object cannot be recast'
            with self.__lock:
                self.__var = var

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

        def __init__(self, instance: 'Tui'):
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

            self._history: List[str] = []
           
            self._registered: Dict[str, Tuple[Callable[..., Any], str]] = {}
            self._mode = self.CMD_MODE_NORMAL
            self._cursor = 0

        def register_command(self, command_name: str, function: Callable[..., Any], tooltip: str = ''):
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

        def get_hints(self) -> List[Tuple[str, str]]:
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
        def history(self) -> List[str]:
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

    cmd_prompt: str = '$ '

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

        self._error_code: int = 0

        # Setting up thread locks
        self._prompt_lock = threading.Lock()
        self._shell_lock = threading.Lock()
        self._print_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._widget_lock = threading.Lock()
        self._running_lock = threading.Lock()

        # setting up dispatch queue for handling keystrokes
        self._dispatch_queue: LifoQueue[Any] = queue.LifoQueue()
        self._input_queue: LifoQueue[Any] = queue.LifoQueue()

        # Banner String, needs to be trimmed, currently static
        banner_trim = 30
        self._banner = banner[:banner_trim]

        # Set up the for running the __res_util as a parallel thread
        self._psutil = self.__Lockable(str)
        threading.Thread(target=self._res_util, daemon=True).start()

         # Tabs - tuple of name, window, buffer, cursor position
        self._resolution = {}
        self._tabs: Dict[str, _TabEntry] = {}          # predefine collection of tabs
        self._footer: Optional[curses.window] = None    # footer defined separately

        # For running list of widget
        self._widget: Dict[int, object] = {}

        # internal flag to trigger TUI/Application Exit
        self._quit = False

        # Set footer bar size, typically its one for tabs, one for prompt, one for application info,
        # and one each side for the box
        self._footer_height = 5

        # Commands
        self._cmd = self.__Commands(self)
        self._cmd.reset_mask_mode()
        self._cmd.reset_cursor()

        self._is_setup = False
        self._setup()
        self._redraw()

        if not self._is_setup:
            print("Failed setting up TUI Screen\r")
            return None

        # set the default tab
        self._activate('console')
        self._stdscr.refresh()

        # Validation
        try:
            assert self._footer_height >= 5, 'TUI: Malformed Footer Size'
            assert self._footer is not None, 'TUI: Footer not defined'
            assert len([tab for tab in self._tabs if tab in ['console', 'log']]) == 2, 'TUI: Mandatory tabs missing'
            assert len([self._tabs[tab] for tab in self._tabs if self._tabs[tab]['selected']]) == 1, \
                'TUI: Tab not activated correctly'
        except AssertionError as e:
            self._shutdown()
            print(f'TUI: Setup configuration is wrong: {e}\r')
            return

        self.INFO("Initialising TUI environment")

        self._cmd.register_command('clear', self.clear, 'Clears console tab, alternative tab name/all can be specified')
        self._cmd.register_command('demo', self.demo, 'Demonstrates inbuilt widgets & functions of TUI')
        self._cmd.register_command('history', self.history, 'Lists all commands executed')
        self._cmd.register_command('info', self.info, 'Prints system information')
        self._cmd.register_command('help', self.help, 'Prints registered command list and hints')
        self._cmd.register_command('quit', self.exit, 'Closes the application')

        # start the command
        threading.Thread(target=self.shell, daemon=True).start()

    def _refreshfooter(self):
        """_refreshfooter() - prints the footer section on each call"""

        if self._footer is None:
            return  # or handle the missing footer appropriately

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
        cmd_prompt = self.cmd_prompt[:floor(width / 2)]

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


    def _redraw(self, force: bool=False):
        
        if curses.is_term_resized(self._resolution['y'], self._resolution['x']):
            self._create_windows()
        
        if not self._is_setup:
            return
        
        if force:
            self._stdscr.touchwin()

        with self._refresh_lock:
            self._refreshfooter()
            self._refreshtab()

            if self._footer:
                self._footer.refresh()
            
            for tab in self._tabs:
                self._tabs[tab]['win'].refresh()
                if self._tabs[tab]['selected']:
                    self._tabs[tab]['panel'].show()
                    self._tabs[tab]['panel'].top()
                else:
                    self._tabs[tab]['panel'].hide()
            
            
            curses.panel.update_panels()
            curses.doupdate()
            # self._stdscr.refresh()

    @property
    def _activetab(self) -> _TabEntry:
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

    def _activate(self, name: str):
        """_activate - mark 'name' as active tab"""
        if name not in self._tabs:
            self.ERROR('TUI: Activating non available Tab')
            return

        for tab in self._tabs:
            self._tabs[tab]['selected'] = False

        self._tabs[name]['selected'] = True

        # repaint tab and footer
        self._redraw()

    def _calculateResolution(self):
        """_resizeScreen - recalculate layout (width, height, origin y, origin x) with origin on top left corner"""
        curses.update_lines_cols()
        self._resolution = {'x': curses.COLS, 'y': curses.LINES}
        self._tab_coordinates = {'h': self._resolution['y'] - self._footer_height, 'w': self._resolution['x'],
                                 'y': 0, 'x': 0}
        self._footer_coordinates = {'h': self._footer_height, 'w': self._resolution['x'],
                                    'y': self._resolution['y'] - self._footer_height, 'x': 0}

    def _log(self, severity: int, message: str):
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
        
        self._redraw()

    def _create_tab(self, coordinates: dict[str, int]) -> _TabEntry:
        win = curses.newwin(coordinates['h'], coordinates['w'],
                            coordinates['y'], coordinates['x'])
        
        win_panel = curses.panel.new_panel(win)
        
        return {'win': win, 'panel': win_panel, 'buffer': [], 'cursor': 0, 'selected': False }

    def _create_windows(self):
        """_build_windows - creates the _footer and _tab windows, discards old windows"""

        active_tab: list[str] | None = None
        # empty previous window
        if self._footer:
            self._footer = None

        if self._tabs:
            active_tab = [key for key, tab in self._tabs.items() if tab["selected"]]
            self._tabs.clear()

        curses.update_lines_cols()
        if curses.COLS < 80 or curses.LINES < 24:
            self.exit(1)
            self._is_setup = False
            print(f"TUI: Screen size ({curses.COLS} x {curses.LINES}) less than minimum\r")
            return

        self._calculateResolution()

        # creating footer, Cant create tab before that
        self._footer = curses.newwin(self._footer_coordinates['h'], self._footer_coordinates['w'],
                                     self._footer_coordinates['y'], self._footer_coordinates['x'])


        for tab_name in ['console', 'log']:
            name = tab_name.strip()

            # Should not already exist, though we cleared tabs
            if not name or name in self._tabs:
                continue

            # Tab is a tuple of name, window, panel, buffer, cursor position, and selected state
            self._tabs[name] = self._create_tab(self._tab_coordinates)

            # Enabling Scrolling
            self._tabs[name]['win'].scrollok(True)
        
        if active_tab:
            self._tabs[active_tab[0]]['selected'] = True
        else:
            self._tabs['console']['selected'] = True
    
    def sig_shutdown(self, signum: int, frame: Optional[FrameType]) -> None:
        self.exit(signum)


    def _shutdown(self):
        """_shutdown - shuts down the curses environment"""

        # if not previously setup - skip
        if not self._is_setup:
            return

        self._is_setup = False

        # empty previous window
        if self._footer:
            self._footer = None

        if self._tabs:
            self._tabs.clear()

        # BEGIN ncurses shutdown/de-initialization...
        try:
            curses.echo()           # Turn echo back on.
            curses.nocbreak()       # Turn off cbreak mode.
            curses.curs_set(True)   # Restore cursor blinking.
        except curses.error: pass

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

        if not hasattr(self, '_stdscr') or not self._stdscr:
            # let's set up the curses default window
            self._stdscr = curses.initscr()
            curses.update_lines_cols()

            # set minimum to 80x25 screen, if lesser
            # better to exit than print weird or bad calculations
            if curses.COLS < 80 or curses.LINES < 24:
                print(f"TUI: Screen size ({curses.COLS} x {curses.LINES}) less than minimum\r")
                return

        # BEGIN ncurses startup/initialization...
        # Disable echos & enter non-blocking cbreak mode
        # putting try since debugger IDEs sometimes just fail
        try:
            curses.noecho()
            curses.cbreak()
            curses.curs_set(False)      # Turn off blinking cursor
        except curses.error:
            self._is_setup = True       # make sure that shutdown executes
            self._shutdown()
            return

        # Enable color if we can
        if curses.has_colors():
            curses.start_color()
            # Set color pairs
            curses.init_pair(self.COLOR_NORMAL, self.fgColor, self.bgColor)
            curses.init_pair(self.COLOR_REVERSE, self.bgColor, self.fgColor)
            curses.init_pair(self.COLOR_WARNING, self.warningColor, self.bgColor)
            curses.init_pair(self.COLOR_ERROR, self.errorColor, self.bgColor)
            curses.init_pair(self.COLOR_HIGHLIGHT, self.highlightColor, self.bgColor)
            curses.init_pair(self.COLOR_FOOTER, self.fgColor, self.bgFooter)

        # Enable the keypad - also permits decoding of multibyte key sequences,
        self._stdscr.keypad(True)

        # no waiting on getch()
        self._stdscr.nodelay(True)

        # Create windows - footer and tabs
        self._create_windows()

        # END ncurses startup/initialization...
        self._is_setup = True

    def enabletab(self, name: str):
        """enableTab - activate the given tab name. this is public function"""
        # Set current Tab based on name, provided it is valid
        if name in self._tabs:
            self._activate(name)

    def _enable_next_tab(self):
        """enable_next_tab: Finds and enables next tab, rotates to first if we reach the end"""
        tab_list = list(self._tabs)
        active_tab = [tab for tab in self._tabs if self._tabs[tab]['selected']]
        next_tab = None

        if len(active_tab) > 1:
            self.ERROR('TUI: More than one tab active, picking first')
            for tab in active_tab:
                self._tabs[tab]['selected'] = False
            next_tab = tab_list[0]
        elif len(active_tab) == 0:
            next_tab = tab_list[0]
            self.ERROR('TUI: No tab active, picking first')
        else:
            # select next tab on the dict
            try:
                next_tab = tab_list[tab_list.index(active_tab[0]) + 1]
            except (ValueError, IndexError):
                next_tab = tab_list[0]

        self._activate(next_tab)

    def _run(self):
        """run - The main thread which is accepting and dispatches keys"""


        # maintaining internal flag to exit loop on true,
        # resetting value before start
        self._quit = False

        with self._running_lock:
        # main loop
            while not self._quit:

                if not self._is_setup:
                    print("TUI: screen has not been setup properly\r")
                    return
        
                # wait 10ms to avoid 100% CPU usage
                curses.napms(10)

                self._redraw()

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
                        width = width - len(self.cmd_prompt[:floor(width / 2)]) - 2
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
                            self._input_queue.put(self._cmd.current)
                            
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
                        width = width - len(self.cmd_prompt[:floor(width / 2)]) - 2
                        # Only if the input is greater than the available space is cursor position relevant
                        if len(self._cmd.current) > width:
                            if self._cmd.cursor < len(self._cmd.current) - width:
                                self._cmd.inc_cursor()

            # broken out of the loop - clean up
            self._shutdown()

    def INFO(self, message: str):
        """INFO - Prints an info severity log"""
        self._log(self.SEVERITY_INFO, message)

    def WARNING(self, message: str):
        """INFO - Prints a warning severity log"""
        self._log(self.SEVERITY_WARNING, message)

    def ERROR(self, message: str):
        """INFO - Prints an error severity log"""
        self._log(self.SEVERITY_ERROR, message)

    def print(self, message:str, attribute: Optional[int | None] = None):
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
        
        # self._redraw()

    def clear(self, name: str):
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
                self.cmd_prompt = '$'

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
                self.cmd_prompt = '(command under progress)'
                self.INFO(f'Executing "{command}"')
                self._cmd.add_history(command)

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

    def pause(self):
        """pause - pauses the TUI, waits for user to press any key to continue"""
        with self._prompt_lock:

            old_prompt = self.cmd_prompt

            self.cmd_prompt = 'TUI Paused, press any key to continue...'
            
            while True:

                condition = threading.Condition()
                self._dispatch_queue.put(condition)

                # condition is called when user has entered text
                with condition:
                    condition.wait()
                
                try:
                    self._input_queue.get()
                except queue.Empty:
                    self.ERROR('TUI: Condition called but nothing in Input Stack')

                break

            self.cmd_prompt = old_prompt


    def prompt(self, message: str, masked: Optional[bool] = False) -> str:

        """prompt - gets user input and returns as string
        Args:
            message(str): the string printed while waiting for user input
            masked(bool): if True, the input is masked, e.g. for passwords
        Returns:
            string giving out the user input
        """
        
        answer = ''

        with self._prompt_lock:

            # incase there is already something on queue, LIFO
            old_prompt = self.cmd_prompt
            self.cmd_prompt = message
            
            # mask mode is set for PROMPT_PASSWORD and reset when input if received
            if masked:
                self._cmd.set_mask_mode()
            
            #   put condition on dispatch queue
            condition = threading.Condition()
            self._dispatch_queue.put(condition)

            # condition is called when user has entered text
            with condition:
                condition.wait()

            # get text entered
            try:
                answer = self._input_queue.get()
            except queue.Empty:
                self.ERROR('TUI: Condition called but nothing in Input Stack')
            
            self._cmd.reset_mask_mode()
            # reset prompt to old value
            self.cmd_prompt = old_prompt
            
        return answer
    
    def add_dispatch(self, condition: threading.Condition):
        """add_dispatch - adds a condition to the dispatch queue, used by prompt and shell
        Args:
            condition(threading.Condition): the condition to be added to the dispatch queue
        """
        self._dispatch_queue.put(condition)
    
    def wait_dispatch(self, condition: threading.Condition) -> str:
        
        with condition:
            condition.wait()
        
        # get text entered
        try:
            answer:str = self._input_queue.get()
        except queue.Empty:
            self.ERROR('TUI: Condition called but nothing in Input Stack')
            return ""
        
        return answer
    
    def add_widget(self, widget: object) -> int:
        """add_widget - adds a widget to the _widget list, used by __Spinner and __ProgressBar
        Args:
            widget(object): the widget to be added to the _widget list
        """
        widget_id = widget.__hash__()
        with self._widget_lock:
            self._widget[widget_id] = widget
        return widget_id
    
    def del_widget(self, widget_id: int):
        """del_widget - removes a widget from the _widget list
        Args:
            widget_id(int): the id of the widget to be removed from the _widget list
        """
        with self._widget_lock:
            if widget_id not in self._widget:
                self.print(f'TUI: No Widget by id {widget_id}')
                return
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

        spin = Spinner('Starting Demo')
        Prompt(PROMPT_YESNO, 'This is YES NO prompt').get_response()
        Prompt(PROMPT_INPUT, 'This accepts Input string').get_response()
        Prompt(PROMPT_OPTIONS, 'This allows you to select from options', ['yes', 'no']).get_response()
        Prompt(PROMPT_PASSWORD, 'This accepts masked input').get_response()

        bar_max: int = 100

        bar = ProgressBar('Progress Bar Demo', maxvalue=bar_max)

        for i in range(bar_max + 1):
            bar.step(value=1)
            curses.napms(10)
        bar.close(persist=True)

        self.pause()

        spin.done()

    def help(self):
        """help - prints the registered commands and hints"""
        for command in self._cmd.get_hints():
            self.print(f'{command[0]}\t-\t{command[1]}')

    def exit(self, error_code: int = 0):
        """exit - helper function parent to close tui gracefully"""
        self._quit = True
        self._error_code = error_code
        # Give sufficient time to run _shutdown, there is an internal napms(10)
        curses.napms(20)
    
    def run(self):
        threading.Thread(target=self._run, daemon=True).start()
        curses.napms(20)
    
    def wait(self):
        """wait - waits for the TUI to exit, this is a blocking call"""
        with self._running_lock:
            if self._error_code != 0:
                print(f"Exited with error code : {self._error_code}\r\n")


# Prompt Class Constants
PROMPT_YESNO    = 1001
PROMPT_INPUT    = 1002
PROMPT_OPTIONS  = 1003
PROMPT_PASSWORD = 1004
PROMPT_PAUSE    = 1006

tui_instance: Tui | None = None

class Prompt:
    _options: (List[str])
    _type: int
    _message: str
    _response: str

    """Prompt Class
    The Prompt class is used to prompt the user for input, with options for yes/no, input, options, and password.
    It provides a simple interface to get user input in a curses environment.
    Attributes:
        _type (int): The type of prompt, e.g., yes/no, input, options, password.
        _message (str): The message to display in the prompt.
        _options (List[str]): List of options for the prompt if applicable.
        _response (str): The response from the user.
    """
    def __init__(self, prompt_type: int, message: str, options: Optional[List[str]] = None) -> None:
        
        """Initializes the Prompt instance with type, message, and optional options."""
        if tui_instance is None:
            raise RuntimeError("Tui instance not initialized. Please create a Tui instance before using Prompt.")
        
        if options is None:
            self._options = []
        else:
            self._options = options

        # Confirm valid prompt type
        if prompt_type not in [PROMPT_YESNO, PROMPT_OPTIONS, PROMPT_PASSWORD, PROMPT_INPUT, PROMPT_PAUSE]:
            tui_instance.ERROR(f"Invalid prompt type: {prompt_type}")
            raise ValueError(f"Invalid prompt type: {prompt_type}")

        # Cannot call PROMPT_OPTIONS with less than two Options, nothing to choose then
        if prompt_type == PROMPT_OPTIONS and len(self._options) < 2:
            tui_instance.ERROR('Prompt type PROMPT_OPTIONS called without sufficient options')
            raise ValueError('Prompt type PROMPT_OPTIONS called without sufficient options')
        
        self._type = prompt_type
        self._message = message

        if prompt_type == PROMPT_YESNO:
                self._message += ' (y/n):'
        elif prompt_type == PROMPT_OPTIONS:
                self._message += str(options)
        elif prompt_type == PROMPT_PAUSE:
                self._message += ' (Press any key to continue...)'
        else:
            self._message = message
        
        
    def get_response(self) -> str:
        """get_response - gets user input and returns as string
        Returns:
            string giving out the user input
        """
        if tui_instance is None:
            raise RuntimeError("Tui instance not initialized. Please create a Tui instance before using Prompt.")
        
        while True:
            masked = (self._type == PROMPT_PASSWORD)
            response = tui_instance.prompt(self._message, masked)

            if self._type == PROMPT_OPTIONS and response not in self._options:
                tui_instance.print('TUI Prompt: Only answers within the option provided are permitted')
                continue

            if self._type == PROMPT_YESNO and response not in ['y', 'Y', 'n', 'N', 'yes', 'Yes', 'no', 'No']:
                tui_instance.print('TUI Prompt: Only answers related to yes/no are permitted')
                continue

            break

        # for PROMPT_PASSWORD print masked content of same length else clear text
        if masked:
            tui_instance.print(self._message + ' ' + '*' * len(response))
        else:
            tui_instance.print(self._message + ' ' + response)
        
        return response

class ProgressBar:
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

    _fmt:str
    _label: str
    _value: int
    _max: int
    _itr_label: str
    _state: int
    _time: int
    _scale_factor: str
    _bar_width: int
    _widget_id: int

    def __init__(self, label: str, itr_label: str = 'it/s', bar_width: int = 40, scale_factor: Optional[str] = '',
                    maxvalue: int = 100, fmt: str = ''):
        """Initializes the instance of Progres bar
        Args:
            label(str): the label to be printed as per bar format
            itr_label(str): the suffix for the rate, may be prefixed with scale factor
            bar_width(int): the width of the bar portion only, [...] for example are not included in this sizing
            scale_factor(str): option between None (autoscale), 'K', 'M' & 'G' and scales the rate accordingly.
        """

        """Initializes the Prompt instance with type, message, and optional options."""
        if tui_instance is None:
            raise RuntimeError("Tui instance not initialized. Please create a Tui instance before using Prompt.")

        self._label = label[:20]
        self._value = 0

        if not maxvalue:
            maxvalue = 100
        self._max = maxvalue

        self._itr_label = itr_label[:6]
        self._state = self.RUNNING

        self._time = time.time_ns()

        if scale_factor not in ['', 'K', 'M', 'G']:
            scale_factor = ''
        self._scale_factor = scale_factor

        if bar_width < 10:
            bar_width = 10
        elif bar_width > 40:
            bar_width = 40
        self._bar_width = bar_width

        if not fmt:
            self._fmt = '{percentage:3.0f}%[{bar}]{value}/{total} : {rate} - {label}'
        
        self._widget_id = tui_instance.add_widget(self)

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
        scale_factor :str = ''

        # if None, Autoscale
        if self._scale_factor == '':
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

    def step(self, value:int=1):
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

    def close(self, persist: Optional[bool] = False):
        """To close actions on progress bar """
        self._state = self.STOPPED
        if tui_instance is None:
            raise RuntimeError("Tui instance not initialized. Please create a Tui instance before using Prompt.")
        
        if persist:
            tui_instance.print(str(self))

        tui_instance.del_widget(self._widget_id)

    def reset(self):
        """Resets the timer for rate calculation"""
        self._value = 0
        self._time = time.time_ns()

class Spinner:
    """ Internal class for Spinner
    Presents a spinner with given character sequence
    Attributes:
        _message(str): the message printed as action of spinner, trimmed to 70 characters
        _lock: threading lock to keep changes atomic
        _position(int): index in character array presenting position of the spinner
        _running(bool): maintains running state of the Spinner
    """
    _message: str
    _lock: threading.Lock
    _position: int
    _running: bool
    _widget_id: int

    # Can pick more from
    # https://stackoverflow.com/questions/2685435/cooler-ascii-spinners
    ASCII_CHAR = ['⣾', '⣽', '⣻', '⢿', '⡿', '⣟', '⣯', '⣷']

    def __init__(self, message: str):
        
        if tui_instance is None:
            raise RuntimeError("Tui instance not initialized. Please create a Tui instance before using Spinner.")  
        
        self._message: str = message[:70]
        self._lock = threading.Lock()
        self._position: int = 0
        self._running = True

        # starts the threat which survives till the spinner is running
        threading.Thread(target=self._step, daemon=True).start()
        self._widget_id = tui_instance.add_widget(self)

    def _step(self):
        """Continuous thread which updates the suffix character till _running is true"""
        while self._running:
            time.sleep(0.1)
            with self._lock:
                self._position = (self._position + 1) % len(self.ASCII_CHAR)

    def done(self):
        """Stopping the Spinner"""
        self._running = False

        if tui_instance is None:
            raise RuntimeError("Tui instance not initialized. Please create a Tui instance before using Prompt.")
        
        tui_instance.del_widget(self._widget_id)
        tui_instance.print(self._message + '... Done')

    @property
    def message(self) -> str:
        return self._message

    def __str__(self) -> str:
        """Return str description of Spinner"""
        with self._lock:
            return self._message + ' ' + self.ASCII_CHAR[self._position]        

# test function - can run this file separately 
if __name__ == '__main__':
    import tui
    tui = Tui("Athena Build Environment v0.1")
    tui_instance = tui

    # Register the signal handler for SIGINT (Ctrl+C)
    signal.signal(signal.SIGINT, tui.sig_shutdown)
    tui.run()
    tui.wait()
