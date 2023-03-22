#  Copyright (c) 2023. Harkirat S Virk <harkiratsvirk@gmail.com>
#
#  This program comes with ABSOLUTELY NO WARRANTY; for details see COPYING.
#  This is free software, and you are welcome to redistribute it under certain conditions; see COPYING for details.

import curses
from curses.textpad import Textbox, rectangle


class Tui:
    BOX_WIDTH = 1

    SEVERITY_ERROR = 1
    SEVERITY_WARNING = 2
    SEVERITY_INFO = 3

    def __init__(self):
        # collection of tabs - tuple of name, window, buffer, cursor position
        self.__tabs = {}

        # this is just to make tab switching easier, reflects the same as __tabs
        self.__tabindex = []

        # footer
        self.__footer = None

        # Commands
        self.__cmd_current = ''
        self.__cmd_history = []
        self.__registered_cmd = {}

        # let's set up the curses default window
        self.stdscr = curses.initscr()

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
        # currently only for default window
        self.stdscr.keypad(True)

        # Set color pairs
        # TODO: load from tui.conf else use defaults
        self.__bgColor = curses.COLOR_BLACK
        self.__bgFooter = curses.COLOR_BLUE
        self.__fgColor = curses.COLOR_WHITE
        self.__warningColor = curses.COLOR_YELLOW
        self.__errorColor = curses.COLOR_RED
        self.__highlightColor = curses.COLOR_GREEN

        self.COLOR_NORMAL = 1
        self.COLOR_REVERSE = 2
        self.COLOR_WARNING = 3
        self.COLOR_ERROR = 4
        self.COLOR_HIGHLIGHT = 5
        self.COLOR_FOOTER = 6
        curses.init_pair(self.COLOR_NORMAL, self.__fgColor, self.__bgColor)
        curses.init_pair(self.COLOR_REVERSE, self.__bgColor, self.__fgColor)
        curses.init_pair(self.COLOR_WARNING, self.__warningColor, self.__bgColor)
        curses.init_pair(self.COLOR_ERROR, self.__errorColor, self.__bgColor)
        curses.init_pair(self.COLOR_HIGHLIGHT, self.__highlightColor, self.__bgColor)
        curses.init_pair(self.COLOR_FOOTER, self.__fgColor, self.__bgFooter)

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
        self.stdscr.refresh()

        self.register_command('clear', self.clear)

    def __refresh__(self):
        __tab_tooltip = "Use Tab Number to rotate through Tabs"
        __tab_prefix = 'Tabs:'

        # print tab list & tooltip
        self.__footer.erase()
        self.__footer.bkgd(curses.color_pair(self.COLOR_FOOTER))
        self.__footer.box()
        self.__footer.addstr(2, self.__resolution['x'] - len(__tab_tooltip) - self.BOX_WIDTH, __tab_tooltip)
        self.__footer.addstr(1, self.BOX_WIDTH, '>' + self.__cmd_current)

        # we should have written till
        self.__footer.addstr(2, self.BOX_WIDTH, __tab_prefix)
        __index = self.BOX_WIDTH + len(__tab_prefix)

        for tab in self.__tabs:
            label = ' | ' + tab + ' | '
            if self.__tabs[tab]['selected']:
                self.__footer.addstr(2, __index, label, curses.A_REVERSE)
            else:
                self.__footer.addstr(2, __index, label)
            __index += len(label)

        self.__footer.refresh()

        # printing the Active tab only
        active_tab = self.__activetab__

        buffer = active_tab['buffer']
        window = active_tab['win']
        cursor = min(active_tab['cursor'], len(buffer))
        # cursor = min(cursor)
        window.erase()
        for i in range(cursor):
            line = buffer[i]
            window.addstr(line[0], line[1])
        window.refresh()


    @property
    def __activetab__(self) -> {}:
        active_tab = [self.__tabs[tab] for tab in self.__tabs if self.__tabs[tab]['selected']]
        assert len(active_tab) == 1, 'TUI: Active State for tabs inconsistent'
        active_tab = active_tab[0]
        return active_tab

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
        self.INFO(f'Executed command "{cmd}"\n')
        cmd_parts = cmd.split()
        if len(cmd_parts) > 0:
            function_name = cmd_parts[0]
            function_args  = cmd_parts[1:]
            if function_name not in self.__registered_cmd:
                self.ERROR(f'Command {function_name} not registered')
                return

            function = self.__registered_cmd[function_name]
            try:
                function(*function_args)
            except:
                self.ERROR(f"Error: {function_name} takes {function.__code__.co_argcount} "
                           f"arguments but {len(function_args)} were given")

        pass

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
            c = self.stdscr.getkey()
            activetab = self.__activetab__
            if c != -1:
                if c == 'KEY_UP':
                    if activetab['cursor'] > self.__tab_coordinates['h']:
                        activetab['cursor'] -= 1
                        self.__refresh__()
                elif c == 'KEY_DOWN':
                    activetab['cursor'] = min(len(activetab['buffer']), activetab['cursor'] + 1)
                    self.__refresh__()
                elif c == '\t':
                    # switch to next Tab on Alt
                    self.enable_next_tab()
                    continue
                elif c == '\n':
                    # Command has been completed
                    if not self.__cmd_current.strip() == '':
                        # Special Case
                        if self.__cmd_current.strip() in ['quit', 'exit', 'q']:
                            __quit = True
                            continue
                        self.print(self.__cmd_current + '\n', curses.color_pair(self.COLOR_HIGHLIGHT))
                        self.__cmd_history.append(self.__cmd_current)
                        self.__executecmd__(self.__cmd_current)
                    self.__cmd_current = ''
                    self.__refresh__()

                else:
                    self.__cmd_current += c
                    self.__refresh__()
            else:
                curses.napms(10)  # wait 10ms to avoid 100% CPU usage

        # clean up
        self.__shutdown__()

    def __log__(self, severity, message):
        assert(severity in [self.SEVERITY_ERROR, self.SEVERITY_WARNING, self.SEVERITY_INFO]), \
            f'TUI: Incorrect Severity {severity} defined'

        attribute = curses.color_pair(self.COLOR_NORMAL)
        if severity == self.SEVERITY_ERROR:
            attribute = curses.color_pair(self.COLOR_ERROR)
        elif severity == self.SEVERITY_WARNING:
            attribute = curses.color_pair(self.COLOR_WARNING)

        logger = self.__tabs['log']
        logger['buffer'].append((message, attribute))
        logger['cursor'] = len(logger['buffer'])
        self.__refresh__()

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

        console['buffer'].append((message, attribute))
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
            self.ERROR(f'Attempted to clear non-existent tab {name}')

    def register_command(self, command_name: str, function):
        if command_name.strip() == '':
            self.ERROR('Registering Empty Command')
            return
        elif command_name in self.__registered_cmd:
            self.INFO(f'Registering duplicate command {command_name}, Ignored')
            return
        else:
            self.__registered_cmd[command_name] = function


# Main function
if __name__ == '__main__':
    tui = Tui()

    tui.run()
