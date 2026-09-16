"""Command-line and Finder Open With entry points for the offline viewer."""
import argparse
import sys
from PySide6.QtCore import QEvent, QTimer
from PySide6.QtWidgets import QApplication
from cbrain_studio.suite.ui_common import configure
from cbrain_studio.suite.viewer import Viewer


class ViewerApplication(QApplication):
    def __init__(self, args):
        super().__init__(args)
        self.window = None
        self.pending_paths = []
        self.dispatch = QTimer(self)
        self.dispatch.setInterval(100)
        self.dispatch.timeout.connect(self.deliver_files)

    def event(self, event):
        if event.type() == QEvent.FileOpen:
            path = event.file()
            if path:
                self.pending_paths.append(path)
                self.dispatch.start()
            return True
        return super().event(event)

    def deliver_files(self):
        if self.window and not self.window.busy and self.pending_paths:
            paths, self.pending_paths = self.pending_paths, []
            self.dispatch.stop()
            self.window.open_paths(paths)
            self.window.show()
            self.window.raise_()


def run():
    parser = argparse.ArgumentParser(description='CBRAIN HDF5 recording viewer')
    parser.add_argument('files', nargs='*')
    parser.add_argument('--smoke-test', metavar='PNG')
    args = parser.parse_args()
    app = ViewerApplication(sys.argv)
    configure(app)
    window = Viewer()
    app.window = window
    window.show()
    if args.files:
        QTimer.singleShot(0, lambda: window.open_paths(args.files))
    if args.smoke_test:
        timer = QTimer(app)
        def capture():
            if window.busy or window.render_job or window.debounce.isActive():
                return
            if args.files and (window.recording is None or window.plot.frame is None):
                print(window.status.text(), file=sys.stderr)
                app.exit(2)
                return
            if not window.grab().save(args.smoke_test):
                app.exit(3)
                return
            timer.stop()
            window.close()
            app.quit()
        timer.timeout.connect(capture)
        timer.start(250)
        QTimer.singleShot(30000, lambda: app.exit(4))
    return app.exec()
