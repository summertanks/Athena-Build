import npyscreen


class FormObject(npyscreen.ActionFormV2):
    def create(self):
        self.add(npyscreen.TitleSliderPercent, accuracy=0, out_of=100, name="Slider")
        pass

    def afterEditing(self):
        self.parentApp.setNextForm(None)


class App(npyscreen.NPSAppManaged):

    def onStart(self):
        self.addForm('MAIN', FormObject, name='Athena Linux')

    pass


def screen_build():
    # Everything setup, can start main loop
    app = App().run()


# Main function
if __name__ == '__main__':
    screen_build()
