#  Copyright (c) 2023. Harkirat S Virk <harkiratsvirk@gmail.com>
#
#  This program comes with ABSOLUTELY NO WARRANTY; for details see COPYING.
#  This is free software, and you are welcome to redistribute it under certain conditions; see COPYING for details.

import curses
from curses import wrapper


class Tui:

    def __init__(self):
        # collection of tabs
        self.__tabs = {}
        self.__tab_index = []
        self.__selected_tab = None

        # footer
        self.__footer = None
        self.__tab_name_str = ''
        self.__tab_tooltip = "Use Alt + Tab to select Tabs, alternatively use Alt + Tab Number"

        # Commands
        self.__cmd_current = ''
        self.__cmd_history = []

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
        self.__fgColor = curses.COLOR_WHITE
        self.__warningColor = curses.COLOR_YELLOW
        self.__errorColor = curses.COLOR_RED
        self.__highlightColor = curses.COLOR_GREEN

        self.COLOR_NORMAL = 1
        self.COLOR_REVERSE = 2
        self.COLOR_WARNING = 3
        self.COLOR_ERROR = 4
        self.COLOR_HIGHLIGHT = 5
        curses.init_pair(self.COLOR_NORMAL, self.__fgColor, self.__bgColor)
        curses.init_pair(self.COLOR_REVERSE, self.__bgColor, self.__fgColor)
        curses.init_pair(self.COLOR_WARNING, self.__warningColor, self.__bgColor)
        curses.init_pair(self.COLOR_ERROR, self.__errorColor, self.__bgColor)
        curses.init_pair(self.COLOR_HIGHLIGHT, self.__highlightColor, self.__bgColor)

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
        self.addTab("console")
        self.addTab("log")

        # refresh
        self.__refresh__()

        # Validation
        assert len([__tab for __tab in self.__tabs if __tab in ['console', 'log']]) == 2, 'TUI: Mandatory tabs missing'

        self.__tabs['console'].addstr("Initialising TUI environment")
        pass

    def __refresh__(self):
        # print tab list & tooltip
        self.__footer.erase()
        self.__footer.bkgd(curses.color_pair(self.COLOR_REVERSE))
        self.__footer.box()
        self.__footer.addstr(2, 2, self.__tab_name_str)
        self.__footer.addstr(2, self.__resolution['x'] - len(self.__tab_tooltip) - 2, self.__tab_tooltip)
        self.__footer.addstr(1, 1, '>' + self.__cmd_current)
        self.__footer.refresh()

        for __tab in self.__tab_index:
            __tab.refresh()

    def __resizeScreen__(self):
        # calculate layout (width, height, origin y, origin x) with origin on top left corner
        self.__tab_coordinates = {'h': self.__resolution['y'] - self.__footer_height, 'w': self.__resolution['x'],
                                  'y': 0, 'x': 0}
        self.__footer_coordinates = {'h': self.__footer_height, 'w': self.__resolution['x'],
                                     'y': self.__resolution['y'] - self.__footer_height, 'x': 0}
        self.__refresh__()

    def __executecmd__(self, cmd):
        self.__tabs['console'].addstr(cmd + '\n')
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


    def addTab(self, name: str):
        if name != '':
            self.__tab_name_str = ''
            self.__tabs[name] = curses.newwin(self.__tab_coordinates['h'], self.__tab_coordinates['w'],
                                              self.__tab_coordinates['y'], self.__tab_coordinates['x'])
            self.__tab_index.append(self.__tabs[name])

            for __name in self.__tabs:
                self.__tab_name_str += ' | ' + __name + ' | '

            # trim
            self.__tab_name_str = 'Tabs:' + self.__tab_name_str[:self.__resolution['x'] - len(self.__tab_tooltip) - 10]

    def enableTab(self, name):
        # Set current Tab based on name, provided it is valid
        if name in self.__tabs:
            self.__selected_tab = self.__tabs[name]
            self.__selected_tab.activate()

    def run(self):
        __quit = False
        # main loop
        while not __quit:
            # get input
            c = self.__footer.getch()
            if c != -1:
                if c == ord('\t') and curses.KEY_ALTDOWN:
                    # switch to next Tab on Alt+Tab
                    self.__tab_index[self.__selected_tab].deactivate()
                    self.__selected_tab = (self.__selected_tab + 1) % len(self.__tab_index)
                    self.__tab_index[self.__selected_tab].activate()
                elif c == ord('\n'):
                    # Command has been completed
                    # Special Case
                    if self.__cmd_current.strip() in ['quit', 'exit', 'q']:
                        __quit = True
                        continue
                    self.__executecmd__(self.__cmd_current)
                    self.__cmd_history.append(self.__cmd_current)
                    self.__cmd_current = ''
                    self.__refresh__()
                else:
                    self.__cmd_current += chr(c)
                    self.__refresh__()
            else:
                curses.napms(10)  # wait 10ms to avoid 100% CPU usage

        # clean up
        self.__shutdown__()

# Main function
if __name__ == '__main__':
    tui = Tui()
    tui.run()
