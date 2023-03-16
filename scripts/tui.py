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

        # footer
        self.__footer = None
        self.__tab_name_str = ''
        self.__selected_tab = ''
        self.__tab_tooltip = "Use Alt + Tab to select Tabs, alternatively use Alt + Tab Number"

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

        # Set footer bar size, typically its one for tabs, one for prompt, one for application info
        self.__footer_height = 3

        # overkill but to avoid someone randomly changing height


        # calculate layout (width, height, origin y, origin x) with origin on top left corner
        self.__tab_coordinates = ()
        self.__footer_coordinates = ()

        self.__resizeTab__()

        # creating footer, Cant create tab before that
        self.__footer = curses.newwin(self.__footer_coordinates[0], self.__footer_coordinates[1],
                                      self.__footer_coordinates[2], self.__footer_coordinates[3])

        # Validation
        assert self.__footer_height >= 3, 'TUI: Malformed Footer Size'
        assert len([__tab for __tab in self.__tabs if __tab in ['footer', 'console', 'log']]) == 3, \
            'TUI: Mandatory tabs missing'
        assert self.__footer is not None, "TUI: Footer not defined"

        # creating basic tabs
        self.addTab("console")
        self.addTab("log")



        # share functions
        self.refresh = self.stdscr.refresh

        print("Initialising TUI environment")
        pass

    def __resizeTab__(self):
        # calculate layout (width, height, origin y, origin x) with origin on top left corner
        self.__tab_coordinates = (self.__resolution['y'] - self.__footer_height, self.__resolution['x'], 0, 0)
        self.__footer_coordinates = (self.__footer_height, self.__resolution['x'],
                                     self.__resolution['y'] - self.__footer_height, 0)

    def __addTab__(self, name: str, coordinates: ()):
        if name is not '':
            self.__tab_name_str = ''
            self.__tabs[name] = curses.newwin(coordinates[0], coordinates[1], coordinates[2], coordinates[3])
            for __name in self.__tabs:
                self.__tab_name_str += ' | ' + __name + ' | '

            # trim
            self.__tab_name_str = 'Tabs:' + self.__tab_name_str[:self.__resolution['x'] - len(self.__tab_tooltip) - 10]

            # print tab list & tooltip
            self.__footer.addstr( 1, 0, self.__tab_name_str)
            self.__footer.addstr(1, self.__resolution['x'] - len(self.__tab_tooltip), self.__tab_tooltip)


    def addTab(self, name: str):
        self.__addTab__(name, self.__tab_coordinates)



