import os
import platform
from datetime import datetime

import cpuinfo
import npyscreen
import psutil

asciiart_logo = '╔══╦╗╔╗─────────╔╗╔╗\n' \
                '║╔╗║╚╣╚╦═╦═╦╦═╗─║║╠╬═╦╦╦╦╦╗\n' \
                '║╠╣║╔╣║║╩╣║║║╬╚╗║╚╣║║║║║╠║╣\n' \
                '╚╝╚╩═╩╩╩═╩╩═╩══╝╚═╩╩╩═╩═╩╩╝'
header = "Athena Build System"
version = '0.1'

# The tasklist
task_description = ["Building Cache", "Parse Dependencies", "Check Alternate Dependency", "Parse Source Packages",
                    "Source Build Dependency Check", "Download Source files", "Expanding Source Packages",
                    "Building Packages"]


class APP(npyscreen.NPSAppManaged):

    def onStart(self):
        self.addForm('MAIN', MainForm, name=f"{header} v{version}")


class MainForm(npyscreen.SplitForm):
    def create(self):
        pass

    def afterEditing(self):
        self.parentApp.setNextForm(None)


def build_layout() -> Layout:
    # Everything setup, can start main loop
    _layout = Layout(name="root")
    _layout.split(
        Layout(name="header", size=3),
        Layout(name="main", ratio=1),
    )

    _layout["main"].split_row(
        Layout(name="l_main", size=40),
        Layout(name="r_main")
    )

    _layout["l_main"].split_column(
        Layout(name="tasklist"),
        Layout(name="sys_res", size=10)
    )

    _layout["r_main"].split_column(
        Layout(name="console"),
        Layout(name="progress", size=7)
    )

    return _layout


class Header:
    """Display header with clock."""

    def __rich__(self) -> Panel:
        grid = Table.grid(expand=True)
        grid.add_column(justify="left", ratio=1)
        grid.add_column(justify="right")
        grid.add_row(
            "[b]Athena Linux build system[/b]" + f' v{version}',
            datetime.now().ctime(),
        )
        return Panel(grid, style="white on blue")


class SysState:
    def __init__(self):
        pass

    def __rich__(self) -> Panel:
        resource_slider = Progress("{task.description}", BarColumn(),
                                   TextColumn("[progress.percentage]{task.percentage:>3.0f}%"))
        cpu_utilization = psutil.cpu_percent()
        memory_utilization = psutil.virtual_memory().percent
        disk_utilization = psutil.disk_usage(os.getcwd()).percent

        resource_slider.add_task("[green]CPU", completed=int(cpu_utilization), total=100)
        resource_slider.add_task("[green]MEM", completed=int(memory_utilization), total=100)
        resource_slider.add_task("[green]DSK", completed=int(disk_utilization), total=100)
        _system = platform.uname()
        _cpu = cpuinfo.get_cpu_info()
        return Panel(Group(
            f"[green]{_system.system} {_system.machine} {_system.release}", f"{_cpu['brand_raw']}", resource_slider))


class TaskList:

    def __init__(self):
        self.tree = Tree("TaskList")
        for task in task_description:
            self.tree.add(task, highlight=True)

    def set_active(self, index: int):
        if 0 <= index <= len(self.tree.children):
            for node in self.tree.children:
                node.style = ''
            self.tree.children[index].style = 'reverse'

    def __rich__(self) -> Panel:
        return Panel(self.tree)


# Main function
if __name__ == '__main__':
    app = APP().run()
