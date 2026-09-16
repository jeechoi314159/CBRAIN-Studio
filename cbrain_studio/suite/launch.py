from __future__ import annotations
import argparse
import sys
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from cbrain_studio.suite.ui_common import configure

def run(kind):
    parser=argparse.ArgumentParser();parser.add_argument('--smoke-test',metavar='PNG');args=parser.parse_args()
    app=QApplication(sys.argv);configure(app)
    if kind=='studio':
        from cbrain_studio.suite.studio import Studio
        window=Studio()
    elif kind=='setup':
        from cbrain_studio.suite.setup import Setup
        window=Setup()
    else:
        from cbrain_studio.suite.builder import Builder
        window=Builder()
    window.show()
    if args.smoke_test:
        def capture():
            window.grab().save(args.smoke_test);window.close();app.quit()
        QTimer.singleShot(1000,capture)
    return app.exec()
