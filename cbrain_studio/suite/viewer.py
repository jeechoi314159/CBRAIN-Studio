"""Offline CBRAIN HDF5 viewer. No device discovery or recording writes."""
from __future__ import annotations

import json
from pathlib import Path
from PySide6.QtCore import Qt, QTimer, Signal, QPointF, QRectF
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QShortcut, QKeySequence
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QComboBox, QDoubleSpinBox, QSlider, QFileDialog, QPlainTextEdit, QPushButton, QSizePolicy)
import numpy as np

from cbrain_studio.suite.ui_common import Window, Job, button, plot_area
from cbrain_studio.suite.saved_recording import inspect, read_view, Cancelled


class ViewJob(Job):
    def run(self):
        try:
            self.result.emit(self.fn(self.cancel))
        except Cancelled:
            pass
        except Exception as exc:
            self.failed.emit(str(exc))


class SavedPlot(QWidget):
    resized = Signal()

    def __init__(self):
        super().__init__()
        self.recording = None
        self.frame = None
        self.loading = False
        self.amplitude = 0.
        self.cursor = None
        self.setMinimumHeight(300)
        self.setMouseTracking(True)

    def set_frame(self, recording, frame):
        self.recording, self.frame, self.loading = recording, frame, False
        self.setMinimumHeight(max(300, 44 + 96 * len(frame.rows) + (50 if frame.led is not None else 0)))
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()

    def mouseMoveEvent(self, event):
        self.cursor = event.position().x()
        self.update()

    def leaveEvent(self, event):
        self.cursor = None
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor('#ffffff'))
        p.setFont(QFont('Arial', 12))
        f, r = self.frame, self.recording
        if self.loading or f is None:
            p.setPen(QColor('#687b8d'))
            p.drawText(self.rect(), Qt.AlignCenter, '선택 구간을 읽고 있습니다…' if self.loading else 'HDF5 파일을 열거나 이 창으로 끌어 놓으세요.')
            p.end()
            return
        if not f.rows or not f.sample_count:
            p.setPen(QColor('#687b8d'))
            text = '표시할 채널을 선택하세요.' if not f.rows else '이 시간 구간에 저장된 샘플이 없습니다.'
            p.drawText(self.rect(), Qt.AlignCenter, text)
            p.end()
            return
        left, right = 100., float(self.width() - 28)
        width = max(1., right - left)
        xs = left + (f.x - f.start) * width / f.span
        p.setPen(QColor('#263e54'))
        p.setFont(QFont('Arial', 13, QFont.DemiBold))
        p.drawText(18, 26, f'{r.device} · 저장 파형 · {f.start:.3f}–{f.start + f.span:.3f}초')
        colors = ['#2b6d9f', '#228777', '#bc7b27', '#8e57ae', '#aa5353', '#347f95', '#837438', '#586dba']
        for pos, row in enumerate(f.rows):
            top = 44 + pos * 96
            center = top + 38
            lo, hi = f.lo[pos], f.hi[pos]
            valid = np.isfinite(lo) & np.isfinite(hi)
            peak = max(float(np.max(np.abs(lo[valid]))), float(np.max(np.abs(hi[valid])))) if np.any(valid) else 0.
            amp = self.amplitude or max(1., peak * 1.05)
            yscale = 33 / amp
            p.fillRect(QRectF(left, top, width, 76), QColor('#fafbfd'))
            p.setFont(QFont('Arial', 11, QFont.DemiBold))
            p.setPen(QColor(colors[row % len(colors)]))
            p.drawText(12, center - 8, f'CH {r.order[row]}')
            p.setFont(QFont('Arial', 10))
            p.setPen(QColor('#687b8d'))
            p.drawText(12, center + 10, f'±{amp:.1f}')
            p.drawText(12, center + 27, r.units)
            for j in range(6):
                x = left + width * j / 5
                p.setPen(QPen(QColor('#e2e7ed'), 1))
                p.drawLine(QPointF(x, top), QPointF(x, top + 76))
                p.setPen(QColor('#687b8d'))
                p.drawText(QRectF(x - 36, top + 78, 72, 17), Qt.AlignCenter, f'{f.start + f.span*j/5:.2f}')
            p.setPen(QPen(QColor('#ccd7e1'), 1))
            p.drawLine(QPointF(left, center), QPointF(right, center))
            p.save()
            p.setClipRect(QRectF(left, top, width, 76))
            p.setPen(QPen(QColor(colors[row % len(colors)]), 1))
            if f.line:
                path = QPainterPath()
                for i, (x, v) in enumerate(zip(xs, lo)):
                    point = QPointF(float(x), center - float(v) * yscale)
                    if f.breaks[i]:
                        path.moveTo(point)
                        p.drawPoint(point)
                    else:
                        path.lineTo(point)
                p.drawPath(path)
            else:
                for x, a, b in zip(xs[valid], lo[valid], hi[valid]):
                    p.drawLine(QPointF(float(x), center - float(a) * yscale), QPointF(float(x), center - float(b) * yscale))
            p.restore()
        bottom = 44 + len(f.rows) * 96
        if f.led is not None:
            for bit in range(2):
                y = bottom + bit * 20
                p.setPen(QColor('#687b8d'))
                p.drawText(12, y + 12, f'LED {bit + 1}')
                p.fillRect(QRectF(left, y, width, 13), QColor('#e5e9ed'))
                on = (f.led & (1 << bit)) != 0
                p.setPen(QPen(QColor('#dca532' if bit == 0 else '#9368ba'), 2))
                for x in xs[on]:
                    p.drawLine(QPointF(float(x), y), QPointF(float(x), y + 13))
        if self.cursor is not None and left <= self.cursor <= right:
            p.setPen(QPen(QColor('#677383'), 1, Qt.DashLine))
            p.drawLine(QPointF(self.cursor, 38), QPointF(self.cursor, bottom))
            t = f.start + (self.cursor - left) / width * f.span
            p.drawText(QRectF(max(left, min(self.cursor - 60, right - 120)), 5, 120, 25), Qt.AlignCenter, f'{t:.3f}초')
        p.end()


class Viewer(Window):
    def __init__(self):
        super().__init__('CBRAIN Recording Viewer', '저장한 HDF5 파일의 파형을 확인합니다. 시간 구간과 채널을 선택해 자세히 볼 수 있습니다.')
        self.recording = None
        self.files = []
        self.channels = []
        self.render_job = None
        self.pending = None
        self.revision = 0
        self.closing = False
        self.opened = False
        self.quit_btn.setToolTip('파일 뷰어를 종료합니다.')
        self.setAcceptDrops(True)
        row = QHBoxLayout()
        row.addWidget(button('파일 열기', self.choose_files, 'primary'))
        row.addWidget(button('세션 폴더 열기', self.choose_folder))
        self.file_box = QComboBox()
        self.file_box.setMinimumWidth(300)
        self.file_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.file_box.currentIndexChanged.connect(self.select_file)
        row.addWidget(self.file_box, 1)
        self.details_btn = button('파일 정보', self.toggle_details)
        self.details_btn.setCheckable(True)
        row.addWidget(self.details_btn)
        self.export_btn = button('파형 PNG 저장', self.export_png)
        row.addWidget(self.export_btn)
        self.layout.addLayout(row)
        self.info = QLabel('파일 열기를 눌러 .h5 파일을 선택하세요. 여러 파일을 함께 열 수 있습니다.')
        self.info.setObjectName('status')
        self.info.setWordWrap(True)
        self.layout.addWidget(self.info)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(170)
        self.details.hide()
        self.layout.addWidget(self.details)
        channel_bar = QHBoxLayout()
        channel_bar.addWidget(QLabel('표시 채널'))
        self.channel_grid = QGridLayout()
        channel_bar.addLayout(self.channel_grid, 1)
        self.all_btn = button('모두 표시', lambda: self.set_channels(True))
        channel_bar.addWidget(self.all_btn)
        self.layout.addLayout(channel_bar)
        row = QHBoxLayout()
        self.previous = button('◀ 이전 구간', lambda: self.step(-1))
        self.next = button('다음 구간 ▶', lambda: self.step(1))
        row.addWidget(self.previous)
        row.addWidget(self.next)
        row.addWidget(QLabel('시작'))
        self.start = QDoubleSpinBox()
        self.start.setDecimals(3)
        self.start.setSuffix(' 초')
        self.start.setMinimumWidth(135)
        self.start.valueChanged.connect(self.position_changed)
        row.addWidget(self.start)
        row.addWidget(QLabel('표시 구간'))
        self.span = QComboBox()
        for seconds in [.1, .5, 1, 2, 5, 10, 30, 60, 300]:
            self.span.addItem(f'{seconds:g}초', seconds)
        self.span.addItem('전체', None)
        self.span.setCurrentIndex(5)
        self.span.currentIndexChanged.connect(self.span_changed)
        row.addWidget(self.span)
        self.zoom_in = button('확대 +', lambda: self.zoom(True))
        self.zoom_out = button('축소 −', lambda: self.zoom(False))
        row.addWidget(self.zoom_in)
        row.addWidget(self.zoom_out)
        row.addStretch()
        row.addWidget(QLabel('진폭'))
        self.amplitude = QComboBox()
        self.amplitude.addItem('자동', 0.)
        for value in [50, 100, 250, 500, 1000, 2500, 5000]:
            self.amplitude.addItem(f'±{value}', float(value))
        self.amplitude.currentIndexChanged.connect(self.amplitude_changed)
        row.addWidget(self.amplitude)
        self.layout.addLayout(row)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 10000)
        self.slider.valueChanged.connect(self.slider_changed)
        self.layout.addWidget(self.slider)
        self.plot = SavedPlot()
        self.plot.resized.connect(self.queue_view)
        self.layout.addWidget(plot_area(self.plot), 1)
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(90)
        self.debounce.timeout.connect(self.request_view)
        self._shortcuts = [QShortcut(QKeySequence.Open, self, activated=self.choose_files),
                           QShortcut(QKeySequence('PgDown'), self, activated=lambda: self.step(1)),
                           QShortcut(QKeySequence('PgUp'), self, activated=lambda: self.step(-1))]
        self.resize(1440, 940)
        self.update_controls()

    def choose_files(self):
        start = str(self.recording.path.parent) if self.recording else str(Path.home() / 'CBRAIN_recordings')
        paths, _ = QFileDialog.getOpenFileNames(self, '저장 파일 열기', start, 'CBRAIN 기록 (*.h5 *.hdf5)')
        if paths:
            self.set_files(paths)

    def choose_folder(self):
        path = QFileDialog.getExistingDirectory(self, '세션 폴더 열기', str(Path.home() / 'CBRAIN_recordings'))
        if path:
            self.open_paths([path])

    def open_paths(self, paths):
        files = []
        for path in paths:
            p = Path(path).expanduser()
            files.extend(sorted(p.glob('*.h5')) + sorted(p.glob('*.hdf5')) if p.is_dir() else [p])
        if not files:
            self.show_error('선택한 폴더에 .h5 파일이 없습니다.')
            return
        self.set_files(files)

    def set_files(self, paths):
        if self.busy or self.closing:
            return
        files = list(dict.fromkeys(Path(p).expanduser().resolve() for p in paths))
        if not files:
            return
        self.files = files
        self.file_box.blockSignals(True)
        self.file_box.clear()
        for p in files:
            self.file_box.addItem(p.name, str(p))
            self.file_box.setItemData(self.file_box.count() - 1, str(p), Qt.ToolTipRole)
        self.file_box.setCurrentIndex(0)
        self.file_box.blockSignals(False)
        self.select_file(0)

    def select_file(self, index):
        if index < 0 or self.busy or self.closing:
            return
        self.revision += 1
        self.pending = None
        self.debounce.stop()
        if self.render_job:
            self.render_job.cancel.set()
        self.recording = None
        self.opened = False
        for b in self.channels:
            self.channel_grid.removeWidget(b)
            b.deleteLater()
        self.channels = []
        self.plot.frame = None
        self.plot.loading = True
        self.plot.update()
        self.info.setText('기록 정보와 시간축을 확인하고 있습니다…')
        self.details.clear()
        path = self.file_box.itemData(index)
        self.file_box.setToolTip(path)
        self.work('파일을 읽고 있습니다…', lambda cancel: inspect(path, cancel), self.loaded)

    def loaded(self, recording):
        if self.closing:
            return
        self.recording = recording
        for b in self.channels:
            self.channel_grid.removeWidget(b)
            b.deleteLater()
        self.channels = []
        for i, channel in enumerate(recording.order):
            b = QPushButton(f'CH {channel}')
            b.setObjectName('deviceChoice')
            b.setCheckable(True)
            b.setChecked(True)
            b.toggled.connect(self.queue_view)
            self.channel_grid.addWidget(b, i // 8, i % 8)
            self.channels.append(b)
        r = recording
        source = r.attrs.get('sample_source', '미기록')
        self.info.setText(f'{r.device}  ·  {r.attrs.get("dongle_name", "동글 이름 미기록")}  ·  {r.channels}채널 / {r.sr:,} Hz  ·  {r.duration:.3f}초\n'
                          f'채널당 {r.count:,}개 저장  ·  카운터 누락 {r.missing:,}개  ·  신호 출처 {source}'
                          + ('\n확인: ' + ' / '.join(r.warnings) if r.warnings else ''))
        detail = {'파일': str(r.path), '시간축': '첫 저장 샘플을 0초로 한 샘플 카운터 기준',
                  '표시 단위': r.units, '환산값': r.scale, '누락 구간 수': r.gap_count,
                  '유효 샘플 길이(초)': r.count / r.sr, 'LED 기록': r.led_reported, '기록 속성': r.attrs}
        self.details.setPlainText(json.dumps(detail, ensure_ascii=False, indent=2, default=str))
        self.start.blockSignals(True)
        self.start.setValue(0)
        self.start.blockSignals(False)
        self.plot.amplitude = self.amplitude.currentData()
        self.opened = True

    def _finished(self):
        super()._finished()
        if self.closing:
            QTimer.singleShot(0, self.close)
        elif self.opened:
            self.opened = False
            self.span_changed()
        elif self.recording is None:
            self.plot.loading = False
            self.plot.update()
            self.info.setText('파일을 열지 못했습니다. 아래 메시지를 확인하거나 다른 파일을 선택하세요.')

    def visible_span(self):
        if not self.recording:
            return 1.
        return max(1 / self.recording.sr, min(self.recording.duration, self.span.currentData() or self.recording.duration))

    def max_start(self):
        return max(0., self.recording.duration - self.visible_span()) if self.recording else 0.

    def span_changed(self, *_):
        self.start.setRange(0., self.max_start())
        self.start.setSingleStep(max(.01, self.visible_span() / 10))
        self.position_changed()

    def position_changed(self, *_):
        self.slider.blockSignals(True)
        self.slider.setValue(round(self.start.value() / self.max_start() * 10000) if self.max_start() else 0)
        self.slider.blockSignals(False)
        self.update_controls()
        self.queue_view()

    def slider_changed(self, value):
        self.start.setValue(self.max_start() * value / 10000)

    def step(self, direction):
        self.start.setValue(self.start.value() + direction * self.visible_span())

    def zoom(self, inward):
        current = self.visible_span()
        values = [self.span.itemData(i) for i in range(self.span.count() - 1)]
        candidates = [x for x in values if x < current - 1e-6] if inward else [x for x in values if x > current + 1e-6]
        target = max(candidates) if inward and candidates else min(candidates) if candidates else None
        if not inward or candidates:
            self.span.setCurrentIndex(self.span.findData(target))

    def amplitude_changed(self, *_):
        self.plot.amplitude = self.amplitude.currentData()
        self.plot.update()

    def set_channels(self, checked):
        for b in self.channels:
            b.blockSignals(True)
            b.setChecked(checked)
            b.blockSignals(False)
        self.queue_view()

    def queue_view(self, *_):
        if self.recording is not None and not self.closing:
            self.revision += 1
            if self.render_job:
                self.render_job.cancel.set()
            self.debounce.start()

    def request_view(self):
        if not self.recording or self.closing:
            return
        rows = [i for i, b in enumerate(self.channels) if b.isChecked()]
        self.pending = (self.revision, self.recording, self.start.value(), self.visible_span(), rows, max(200, self.plot.width() - 128))
        self.plot.loading = True
        self.plot.update()
        self.export_btn.setEnabled(False)
        self.message('선택 구간을 읽고 있습니다…')
        if self.render_job:
            self.render_job.cancel.set()
        else:
            self.start_render()

    def start_render(self):
        if not self.pending or self.closing:
            return
        revision, r, start, span, rows, pixels = self.pending
        self.pending = None
        job = ViewJob(lambda cancel: read_view(r, start, span, rows, pixels, cancel))
        self.render_job = job
        def complete(frame):
            if revision != self.revision or self.closing or r is not self.recording:
                return
            self.plot.set_frame(r, frame)
            self.message(f'{start:.3f}–{start + span:.3f}초 · {frame.sample_count:,}개/채널 · '
                         + ('개별 샘플 표시' if frame.line else '시간 구간별 최소·최대 표시')
                         + ' · 원본 값 보기')
            self.update_controls()
        def failed(message):
            if revision == self.revision and not self.closing:
                self.plot.frame = None
                self.plot.loading = False
                self.plot.update()
                self.show_error(message)
        job.result.connect(complete)
        job.failed.connect(failed)
        job.finished.connect(self.render_finished)
        job.start()

    def render_finished(self):
        job, self.render_job = self.render_job, None
        job.deleteLater()
        if self.closing:
            QTimer.singleShot(0, self.close)
        elif self.pending:
            self.start_render()

    def update_controls(self):
        if not hasattr(self, 'plot'):
            return
        ready = self.recording is not None and not self.busy
        for w in [self.span, self.amplitude, self.all_btn, self.zoom_in, self.zoom_out, self.details_btn]:
            w.setEnabled(ready)
        self.start.setEnabled(ready and self.max_start() > 0)
        self.slider.setEnabled(ready and self.max_start() > 0)
        self.previous.setEnabled(ready and self.start.value() > 0)
        self.next.setEnabled(ready and self.start.value() < self.max_start() - .001)
        self.export_btn.setEnabled(ready and self.plot.frame is not None and not self.plot.loading)

    def toggle_details(self):
        self.details.setVisible(self.details_btn.isChecked())

    def export_png(self):
        if self.plot.frame is None or self.plot.loading or not self.recording:
            return
        path, _ = QFileDialog.getSaveFileName(self, '표시 파형 저장', str(self.recording.path.with_suffix('.png')), 'PNG 이미지 (*.png)')
        if path:
            path = str(Path(path).with_suffix('.png'))
            if not self.plot.grab().save(path):
                self.show_error('이미지를 저장하지 못했습니다.')
            else:
                self.message('파형 이미지를 저장했습니다: ' + path)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and not self.busy:
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.open_paths([url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()])
        event.acceptProposedAction()

    def closeEvent(self, event):
        self.closing = True
        self.debounce.stop()
        self.pending = None
        if self.render_job:
            self.render_job.cancel.set()
        if self.busy or self.render_job:
            self.cancel()
            event.ignore()
            return
        event.accept()
