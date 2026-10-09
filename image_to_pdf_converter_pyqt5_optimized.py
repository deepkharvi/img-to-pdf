import sys
import os
import time
import tempfile
import uuid
import base64
import html as htmlmod
import multiprocessing as mp
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QComboBox, QTableWidget,
    QTableWidgetItem, QFileDialog, QCheckBox, QMessageBox, QProgressBar,
    QRadioButton, QButtonGroup, QGroupBox, QSpinBox
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QFont
from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm

IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.tiff')
JPEG_QUALITY = 90
FLIPBOOK_MAX_DIM = 1800


# ==========================================================================
# MODULE-LEVEL CONVERSION FUNCTIONS
#
# These run inside separate worker PROCESSES (via multiprocessing.Pool),
# not inside the GUI's thread. That is the single biggest fix here: the
# original app did every folder — and every image inside every folder —
# one at a time, on one thread, with the whole app waiting on it. With
# multiprocessing, several folders convert at once across CPU cores, so
# 100-1000 folders finish in minutes instead of grinding the UI to a halt.
#
# NOTE: functions used with multiprocessing.Pool must be plain top-level
# functions (picklable) — they can't be bound methods on a QThread, which
# is why this logic has moved out of the ConversionWorker class.
# ==========================================================================

def _margin_cm(margin_label):
    if "Small" in margin_label:
        return 0.5
    elif "Large" in margin_label:
        return 1.5
    return 1.0


def _permission_hint(output_path):
    return (f"Could not write '{output_path}'. This usually means the file is "
            f"currently open in a PDF viewer/browser, the folder is synced by "
            f"OneDrive and locked, or antivirus is blocking the write. Close "
            f"any program that has this file open, or pick a different output "
            f"folder, then try again.")


def _write_pdf_auto_fit(image_files, folder_path, output_path):
    """Page size matches each image's own size.

    Rewritten to STREAM one page at a time instead of loading every image
    in the folder into a Python list before saving (the original held all
    of them in memory simultaneously — for folders with many large photos
    this is what ran the app out of memory on bigger batches).
    """
    c = None
    try:
        for image_file in image_files:
            img = Image.open(os.path.join(folder_path, image_file)).convert('RGB')
            w, h = img.size
            if c is None:
                c = canvas.Canvas(output_path, pagesize=(w, h))
            else:
                c.setPageSize((w, h))

            temp_path = os.path.join(tempfile.gettempdir(), f"i2p_{uuid.uuid4().hex}.jpg")
            img.save(temp_path, "JPEG", quality=JPEG_QUALITY, optimize=True)
            try:
                c.drawImage(temp_path, 0, 0, width=w, height=h)
                c.showPage()
            finally:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            del img
        if c is not None:
            c.save()
    except PermissionError:
        raise PermissionError(_permission_hint(output_path))


def _write_pdf_stretch_fit(image_files, folder_path, output_path, margin_label):
    page_width, page_height = A4
    margin = _margin_cm(margin_label) * cm
    usable_width = page_width - (margin * 2)
    usable_height = page_height - (margin * 2)

    try:
        c = canvas.Canvas(output_path, pagesize=A4)
    except PermissionError:
        raise PermissionError(_permission_hint(output_path))

    for image_file in image_files:
        img = Image.open(os.path.join(folder_path, image_file)).convert('RGB')
        temp_path = os.path.join(tempfile.gettempdir(), f"i2p_{uuid.uuid4().hex}.jpg")
        img.save(temp_path, "JPEG", quality=JPEG_QUALITY, optimize=True)
        try:
            c.drawImage(temp_path, margin, margin, width=usable_width, height=usable_height)
            c.showPage()
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass
        del img
    c.save()


def _write_pdf_maintain_ratio(image_files, folder_path, output_path, margin_label):
    page_width, page_height = A4
    margin = _margin_cm(margin_label) * cm
    usable_width = page_width - (margin * 2)
    usable_height = page_height - (margin * 2)

    try:
        c = canvas.Canvas(output_path, pagesize=A4)
    except PermissionError:
        raise PermissionError(_permission_hint(output_path))

    for image_file in image_files:
        img = Image.open(os.path.join(folder_path, image_file)).convert('RGB')
        img_width, img_height = img.size

        scale_w = usable_width / img_width
        scale_h = usable_height / img_height
        scale = min(scale_w, scale_h)

        new_width = img_width * scale
        new_height = img_height * scale
        x = margin + (usable_width - new_width) / 2
        y = margin + (usable_height - new_height) / 2

        temp_path = os.path.join(tempfile.gettempdir(), f"i2p_{uuid.uuid4().hex}.jpg")
        img.save(temp_path, "JPEG", quality=JPEG_QUALITY, optimize=True)
        try:
            c.drawImage(temp_path, x, y, width=new_width, height=new_height)
            c.showPage()
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass
        del img
    c.save()


def _build_flipbook_html(folder_name, pages_data, ratio_w=3, ratio_h=4):
    safe_title = htmlmod.escape(folder_name)
    pages_json = "[\n" + ",\n".join(f'"{src}"' for src in pages_data) + "\n]"
    try:
        ratio = max(0.2, min(5.0, float(ratio_w) / float(ratio_h)))
    except (ZeroDivisionError, ValueError):
        ratio = 0.75
    ratio_css = f"{ratio:.5f}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{safe_title} — Flipbook</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * {{ box-sizing: border-box; }}
  html, body {{
    margin: 0; width: 100%; height: 100%;
    background: #e9ebf0;
    color: #2b2f36;
    font-family: 'Segoe UI', Arial, sans-serif;
    overflow: hidden;
  }}
  body {{ position: relative; }}
  .stage {{
    position: fixed; inset: 0;
    display: flex; align-items: center; justify-content: center;
    -webkit-perspective: 2600px;
    perspective: 2600px;
    background: radial-gradient(ellipse at center, #f1f2f5 0%, #dfe2e8 100%);
    padding: 24px;
  }}
  .book {{
    position: relative;
    width: min(86vw, calc(84vh * {ratio_css}), 860px);
    aspect-ratio: {ratio_css};
    max-height: 88vh;
    border-radius: 3px;
    box-shadow:
      0 2px 6px rgba(0,0,0,.12),
      0 18px 40px rgba(0,0,0,.22),
      0 0 0 1px rgba(0,0,0,.04);
    background: #ffffff;
  }}
  .book::before {{
    content: ''; position: absolute; inset: 0 0 0 0;
    background: linear-gradient(to right, rgba(0,0,0,.10), rgba(0,0,0,0) 3%);
    pointer-events: none; z-index: 4; border-radius: 3px;
  }}
  .page {{
    position: absolute; inset: 0;
    -webkit-transform-style: preserve-3d;
    transform-style: preserve-3d;
    transform-origin: left center;
    -webkit-transition: -webkit-transform .75s cubic-bezier(.4,.1,.25,1);
    transition: transform .75s cubic-bezier(.4,.1,.25,1);
    border-radius: 3px; overflow: hidden;
  }}
  .page .face {{
    position: absolute; inset: 0;
    -webkit-backface-visibility: hidden;
    backface-visibility: hidden;
    background: #ffffff;
    display: flex; align-items: center; justify-content: center;
  }}
  .page .face img {{
    width: 100%; height: 100%; object-fit: contain;
    user-select: none; -webkit-user-drag: none;
  }}
  .page .back {{ -webkit-transform: rotateY(180deg); transform: rotateY(180deg); background: #ffffff; }}
  .page.flipped {{ -webkit-transform: rotateY(-180deg); transform: rotateY(-180deg); }}
  .page.behind {{ visibility: hidden; }}
  .page .shade {{
    position: absolute; inset: 0;
    pointer-events: none; opacity: 0; z-index: 3;
    background: linear-gradient(to right, rgba(0,0,0,.30), rgba(0,0,0,0) 45%);
  }}
  .page .curl {{
    position: absolute; inset: 0;
    pointer-events: none; opacity: 0; z-index: 3;
    background: linear-gradient(100deg, rgba(255,255,255,0) 40%, rgba(255,255,255,.55) 50%, rgba(255,255,255,0) 60%);
  }}
  .page.flip-anim .shade {{ animation: shadeSweep .75s ease; }}
  .page.flip-anim .curl {{ animation: curlSweep .75s ease; }}
  @keyframes shadeSweep {{
    0%   {{ opacity: 0; }}
    50%  {{ opacity: .45; }}
    100% {{ opacity: 0; }}
  }}
  @keyframes curlSweep {{
    0%   {{ opacity: 0; }}
    50%  {{ opacity: .8; }}
    100% {{ opacity: 0; }}
  }}
  .nav-btn {{
    position: fixed; top: 50%; transform: translateY(-50%);
    width: 52px; height: 52px; border-radius: 50%;
    border: none; background: rgba(0,0,0,.06); color: #374151;
    font-size: 20px; line-height: 1; cursor: pointer; z-index: 10;
    display: flex; align-items: center; justify-content: center;
    transition: background .15s ease, opacity .15s ease;
  }}
  .nav-btn:hover:not(:disabled) {{ background: rgba(0,0,0,.12); }}
  .nav-btn:disabled {{ opacity: 0; pointer-events: none; }}
  #prevBtn {{ left: 18px; }}
  #nextBtn {{ right: 18px; }}
  .counter {{
    position: fixed; bottom: 14px; left: 50%; transform: translateX(-50%);
    font-size: 12px; color: #8b93a1; z-index: 10; pointer-events: none;
  }}
  .empty {{
    position: fixed; inset: 0; display: flex; align-items: center; justify-content: center;
    font-size: 15px; color: #8b93a1;
  }}
</style>
</head>
<body>
  <div class="stage">
    <div class="book" id="book"></div>
  </div>
  <button class="nav-btn" id="prevBtn" title="Previous page">&#8592;</button>
  <button class="nav-btn" id="nextBtn" title="Next page">&#8594;</button>
  <div class="counter" id="counter">Page 1 / 1</div>

<script>
  const PAGES = {pages_json};
  const FLIP_MS = 750;
  const book = document.getElementById('book');
  const counter = document.getElementById('counter');
  const prevBtn = document.getElementById('prevBtn');
  const nextBtn = document.getElementById('nextBtn');
  let current = 0;
  let animating = false;

  function buildPages() {{
    if (PAGES.length === 0) {{
      book.innerHTML = '<div class="empty">No pages found.</div>';
      return;
    }}
    PAGES.forEach((src) => {{
      const page = document.createElement('div');
      page.className = 'page';
      page.innerHTML =
        '<div class="face front"><img src="' + src + '" draggable="false"></div>' +
        '<div class="face back"></div>' +
        '<div class="shade"></div>' +
        '<div class="curl"></div>';
      book.appendChild(page);
    }});
    updateZ();
    updateUI();
  }}

  function playFlipAnim(page) {{
    page.classList.remove('flip-anim');
    void page.offsetWidth;
    page.classList.add('flip-anim');
    setTimeout(() => page.classList.remove('flip-anim'), FLIP_MS);
  }}

  function goNext() {{
    if (animating || current >= PAGES.length) return;
    animating = true;
    const flippingIndex = current;
    const page = book.children[flippingIndex];
    page.classList.add('flipped');
    playFlipAnim(page);
    current++;
    updateZ(flippingIndex);
    updateUI();
    setTimeout(() => {{
      animating = false;
      updateZ();
    }}, FLIP_MS);
  }}

  function goPrev() {{
    if (animating || current <= 0) return;
    animating = true;
    current--;
    const flippingIndex = current;
    const page = book.children[flippingIndex];
    page.classList.remove('flipped');
    playFlipAnim(page);
    updateZ(flippingIndex);
    updateUI();
    setTimeout(() => {{
      animating = false;
      updateZ();
    }}, FLIP_MS);
  }}

  function updateZ(animatingIndex) {{
    const n = PAGES.length;
    Array.from(book.children).forEach((page, i) => {{
      page.style.zIndex = i < current ? (i + 1) : (2 * n - i);
      if (i === current || i === animatingIndex) {{
        page.classList.remove('behind');
      }} else {{
        page.classList.add('behind');
      }}
    }});
  }}

  function updateUI() {{
    counter.textContent = 'Page ' + Math.min(current + 1, PAGES.length) + ' / ' + PAGES.length;
    prevBtn.disabled = current === 0;
    nextBtn.disabled = current === PAGES.length;
  }}

  document.addEventListener('keydown', (e) => {{
    if (e.key === 'ArrowRight') goNext();
    if (e.key === 'ArrowLeft') goPrev();
  }});
  prevBtn.addEventListener('click', goPrev);
  nextBtn.addEventListener('click', goNext);

  buildPages();
</script>
</body>
</html>
"""


def _write_flipbook_html(image_files, folder_path, output_path, folder_name):
    pages_data = []
    ratio_w, ratio_h = 3, 4

    for idx, image_file in enumerate(image_files):
        img = Image.open(os.path.join(folder_path, image_file)).convert('RGB')
        w, h = img.size
        if idx == 0:
            ratio_w, ratio_h = w, h
        if max(w, h) > FLIPBOOK_MAX_DIM:
            scale = FLIPBOOK_MAX_DIM / max(w, h)
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

        import io
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88)
        b64 = base64.b64encode(buf.getvalue()).decode('ascii')
        pages_data.append(f"data:image/jpeg;base64,{b64}")
        del img

    html_content = _build_flipbook_html(folder_name, pages_data, ratio_w, ratio_h)
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
    except PermissionError:
        raise PermissionError(_permission_hint(output_path))


def convert_one_folder(args):
    """Runs in a worker PROCESS. Converts a single folder and returns a
    small picklable result dict — this is the unit of work handed out by
    the multiprocessing.Pool, one per folder, in parallel across cores."""
    (folder_path, output_folder, fitting_mode, margin_label,
     generate_pdf, generate_flipbook) = args

    folder_name = os.path.basename(folder_path)
    try:
        image_files = sorted(
            f for f in os.listdir(folder_path) if f.lower().endswith(IMAGE_EXTS)
        )
        if not image_files:
            return {'folder': folder_name, 'status': 'skip', 'reason': 'No images found'}

        if generate_pdf:
            pdf_path = os.path.join(output_folder, f"{folder_name}.pdf")
            if fitting_mode == "auto":
                _write_pdf_auto_fit(image_files, folder_path, pdf_path)
            elif fitting_mode == "stretch":
                _write_pdf_stretch_fit(image_files, folder_path, pdf_path, margin_label)
            else:
                _write_pdf_maintain_ratio(image_files, folder_path, pdf_path, margin_label)

        if generate_flipbook:
            html_path = os.path.join(output_folder, f"{folder_name}_flipbook.html")
            _write_flipbook_html(image_files, folder_path, html_path, folder_name)

        return {'folder': folder_name, 'status': 'success', 'images': len(image_files)}
    except Exception as e:
        return {'folder': folder_name, 'status': 'error', 'error': str(e)}


# ==========================================================================
# BACKGROUND THREADS (run inside the GUI process, keep the window responsive)
# ==========================================================================

class FolderScanWorker(QThread):
    """Scans subfolders for images off the GUI thread.

    The original app did this scan synchronously inside browse_source_folder,
    calling os.listdir() on every subfolder before the window could repaint.
    With 100-1000 subfolders that alone could freeze the UI for a long time
    before conversion even started. Moving it to a QThread fixes that.
    """
    result_ready = pyqtSignal(list)   # list of (folder_name, image_count)
    scan_error = pyqtSignal(str)

    def __init__(self, source_folder):
        super().__init__()
        self.source_folder = source_folder

    def run(self):
        results = []
        try:
            for name in sorted(os.listdir(self.source_folder)):
                path = os.path.join(self.source_folder, name)
                if os.path.isdir(path):
                    try:
                        count = sum(1 for f in os.listdir(path) if f.lower().endswith(IMAGE_EXTS))
                    except Exception:
                        count = 0
                    if count > 0:
                        results.append((name, count))
            self.result_ready.emit(results)
        except Exception as e:
            self.scan_error.emit(str(e))


class ConversionSupervisor(QThread):
    """Owns a multiprocessing.Pool and reports progress ONCE PER FOLDER.

    Two changes from the original ConversionWorker are what make 100+
    folders practical:
      1. Real parallelism — multiple folders convert simultaneously across
         CPU cores instead of one image at a time on a single thread.
      2. Progress is reported per FOLDER, not per IMAGE. The original
         emitted a Qt signal after every single image; for 100 folders with
         50+ images each that's thousands of signal emits flooding the
         event loop and stalling the UI even after conversion sped up.
    """
    folder_done = pyqtSignal(dict)
    progress = pyqtSignal(int, int)          # completed, total
    finished_all = pyqtSignal(dict)          # summary dict
    error = pyqtSignal(str)

    def __init__(self, folders, output_folder, fitting_mode, margin_label,
                 generate_pdf, generate_flipbook, processes):
        super().__init__()
        self.folders = folders
        self.output_folder = output_folder
        self.fitting_mode = fitting_mode
        self.margin_label = margin_label
        self.generate_pdf = generate_pdf
        self.generate_flipbook = generate_flipbook
        self.processes = max(1, processes)
        self._cancel_requested = False

    def cancel(self):
        self._cancel_requested = True

    def run(self):
        try:
            os.makedirs(self.output_folder, exist_ok=True)

            worker_args = [
                (folder, self.output_folder, self.fitting_mode, self.margin_label,
                 self.generate_pdf, self.generate_flipbook)
                for folder in self.folders
            ]
            total = len(worker_args)
            if total == 0:
                self.error.emit("No folders to convert!")
                return

            successful = failed = skipped = 0
            start_time = time.time()

            with mp.Pool(self.processes) as pool:
                for i, result in enumerate(
                    pool.imap_unordered(convert_one_folder, worker_args, chunksize=2), 1
                ):
                    if self._cancel_requested:
                        pool.terminate()
                        self.finished_all.emit({'cancelled': True})
                        return

                    if result['status'] == 'success':
                        successful += 1
                    elif result['status'] == 'skip':
                        skipped += 1
                    else:
                        failed += 1

                    self.folder_done.emit(result)
                    self.progress.emit(i, total)

            elapsed = time.time() - start_time
            self.finished_all.emit({
                'successful': successful, 'failed': failed, 'skipped': skipped,
                'elapsed': elapsed,
            })
        except Exception as e:
            self.error.emit(f"Conversion error: {str(e)}")


# ==========================================================================
# MAIN WINDOW
# ==========================================================================

class ImageToPDFApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.source_folder = ""
        self.output_folder = ""
        self.selected_folders = []
        self.scan_worker = None
        self.worker = None
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle("Image to PDF Converter")
        self.setGeometry(100, 100, 1100, 860)
        self.setStyleSheet("""
            QMainWindow { background-color: #F4F6F9; }
            QGroupBox {
                font-weight: bold;
                font-size: 11pt;
                color: #1F2937;
                border: 1px solid #D8DEE6;
                border-radius: 8px;
                margin-top: 14px;
                padding-top: 14px;
                background-color: #FFFFFF;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
            }
            QLabel { color: #374151; }
            QLineEdit {
                padding: 6px;
                border: 1px solid #D1D5DB;
                border-radius: 5px;
                background-color: #FAFAFA;
            }
            QComboBox, QSpinBox {
                padding: 5px;
                border: 1px solid #D1D5DB;
                border-radius: 5px;
            }
            QPushButton {
                padding: 7px 14px;
                border-radius: 6px;
                border: 1px solid #D1D5DB;
                background-color: #FFFFFF;
            }
            QPushButton:hover { background-color: #F0F4FF; }
            QPushButton:disabled { color: #9CA3AF; background-color: #F3F4F6; }
            QTableWidget {
                border: 1px solid #D8DEE6;
                border-radius: 6px;
                gridline-color: #EEF1F5;
                background-color: #FFFFFF;
            }
            QHeaderView::section {
                background-color: #EEF1F5;
                padding: 6px;
                border: none;
                font-weight: bold;
                color: #374151;
            }
            QProgressBar {
                border: 1px solid #D1D5DB;
                border-radius: 6px;
                text-align: center;
                height: 22px;
                background-color: #EEF1F5;
            }
            QProgressBar::chunk {
                background-color: #0052CC;
                border-radius: 5px;
            }
        """)

        main_widget = QWidget()
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(18, 16, 18, 16)
        main_layout.setSpacing(14)

        title = QLabel("Image → PDF & Flipbook Converter")
        title_font = QFont()
        title_font.setPointSize(19)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setStyleSheet("color: #111827;")
        main_layout.addWidget(title)

        subtitle = QLabel("Convert folders of images into PDF documents and/or interactive HTML flipbooks — "
                           "one output per subfolder. Now processes multiple folders in parallel.")
        subtitle.setStyleSheet("color: #6B7280;")
        main_layout.addWidget(subtitle)

        # --- Folders group ---
        folders_group = QGroupBox("FOLDERS")
        folders_layout = QVBoxLayout()
        folders_layout.setSpacing(10)

        source_layout = QHBoxLayout()
        source_label = QLabel("Source folder:")
        source_label.setFixedWidth(110)
        source_layout.addWidget(source_label)
        self.source_input = QLineEdit()
        self.source_input.setReadOnly(True)
        source_layout.addWidget(self.source_input)
        self.browse_source_btn = QPushButton("Browse...")
        self.browse_source_btn.clicked.connect(self.browse_source_folder)
        source_layout.addWidget(self.browse_source_btn)
        folders_layout.addLayout(source_layout)

        output_layout = QHBoxLayout()
        output_label = QLabel("Output folder:")
        output_label.setFixedWidth(110)
        output_layout.addWidget(output_label)
        self.output_input = QLineEdit()
        self.output_input.setReadOnly(True)
        output_layout.addWidget(self.output_input)
        output_btn = QPushButton("Browse...")
        output_btn.clicked.connect(self.browse_output_folder)
        output_layout.addWidget(output_btn)
        folders_layout.addLayout(output_layout)

        self.scan_status_label = QLabel("")
        self.scan_status_label.setStyleSheet("color: #6B7280; font-style: italic;")
        folders_layout.addWidget(self.scan_status_label)

        folders_group.setLayout(folders_layout)
        main_layout.addWidget(folders_group)

        # --- Output format group ---
        output_format_group = QGroupBox("OUTPUT FORMAT")
        output_format_layout = QHBoxLayout()
        output_format_layout.setSpacing(20)

        self.output_pdf_check = QCheckBox("PDF Document")
        self.output_pdf_check.setChecked(True)
        self.output_pdf_check.setStyleSheet("font-weight: bold;")
        output_format_layout.addWidget(self.output_pdf_check)

        self.output_flipbook_check = QCheckBox("HTML Flipbook (page-turn effect, opens in any browser)")
        self.output_flipbook_check.setChecked(False)
        output_format_layout.addWidget(self.output_flipbook_check)
        output_format_layout.addStretch()

        output_format_group.setLayout(output_format_layout)
        main_layout.addWidget(output_format_group)

        # --- PDF options group ---
        options_group = QGroupBox("PDF OPTIONS")
        options_layout = QVBoxLayout()
        options_layout.setSpacing(10)

        fitting_label = QLabel("Image fitting mode:")
        fitting_label.setStyleSheet("font-weight: bold;")
        options_layout.addWidget(fitting_label)

        fitting_layout = QHBoxLayout()
        self.fitting_group = QButtonGroup()

        self.auto_fit = QRadioButton("Auto Fit — page matches image size (no white space)")
        self.auto_fit.setChecked(True)
        self.auto_fit.setStyleSheet("font-weight: bold; color: #0052CC;")
        self.fitting_group.addButton(self.auto_fit, 0)
        fitting_layout.addWidget(self.auto_fit)

        self.stretch_fit = QRadioButton("Stretch — fill A4 page (may distort)")
        self.fitting_group.addButton(self.stretch_fit, 1)
        fitting_layout.addWidget(self.stretch_fit)

        self.maintain_fit = QRadioButton("Maintain Ratio — fit on A4 with borders")
        self.fitting_group.addButton(self.maintain_fit, 2)
        fitting_layout.addWidget(self.maintain_fit)

        options_layout.addLayout(fitting_layout)

        row_layout = QHBoxLayout()

        margin_label = QLabel("Page margin:")
        margin_label.setFixedWidth(110)
        row_layout.addWidget(margin_label)
        self.page_margin = QComboBox()
        self.page_margin.addItems(["Small (0.5 cm)", "Normal (1 cm)", "Large (1.5 cm)"])
        self.page_margin.setCurrentIndex(1)
        self.page_margin.setFixedWidth(180)
        row_layout.addWidget(self.page_margin)

        row_layout.addSpacing(20)

        proc_label = QLabel("Parallel folders:")
        row_layout.addWidget(proc_label)
        self.processes_spin = QSpinBox()
        self.processes_spin.setMinimum(1)
        self.processes_spin.setMaximum(max(1, mp.cpu_count() * 2))
        self.processes_spin.setValue(min(4, mp.cpu_count()))
        self.processes_spin.setFixedWidth(70)
        row_layout.addWidget(self.processes_spin)
        cpu_hint = QLabel(f"({mp.cpu_count()} CPU cores detected — this many folders convert at once)")
        cpu_hint.setStyleSheet("color: #9CA3AF;")
        row_layout.addWidget(cpu_hint)

        row_layout.addStretch()
        options_layout.addLayout(row_layout)

        info_label = QLabel("ℹ️  Recommended: 'Auto Fit' gives the best results with mixed-size images. "
                             "(Fitting options apply to PDF output only — the Flipbook always fits each "
                             "image to the page automatically.) Raising 'Parallel folders' speeds up large "
                             "batches but uses more CPU and RAM at once — 4-8 is a good range for most machines.")
        info_label.setStyleSheet(
            "background-color: #E8F4FD; padding: 8px 10px; border-radius: 5px; color: #0052CC;")
        info_label.setWordWrap(True)
        options_layout.addWidget(info_label)

        options_group.setLayout(options_layout)
        main_layout.addWidget(options_group)

        # --- Subfolders group ---
        subfolders_group = QGroupBox("SUBFOLDERS FOUND")
        subfolders_layout = QVBoxLayout()
        subfolders_layout.setSpacing(10)

        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["", "Folder Name", "Images", "Status"])
        self.table.setColumnWidth(0, 30)
        self.table.setColumnWidth(1, 300)
        self.table.setColumnWidth(2, 100)
        self.table.setColumnWidth(3, 140)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(False)
        subfolders_layout.addWidget(self.table)

        select_layout = QHBoxLayout()
        self.select_all_btn = QPushButton("Select All")
        self.select_all_btn.clicked.connect(self.select_all)
        select_layout.addWidget(self.select_all_btn)

        self.select_none_btn = QPushButton("Select None")
        self.select_none_btn.clicked.connect(self.select_none)
        select_layout.addWidget(self.select_none_btn)
        select_layout.addStretch()
        subfolders_layout.addLayout(select_layout)

        subfolders_group.setLayout(subfolders_layout)
        main_layout.addWidget(subfolders_group, stretch=1)

        # --- Convert + Cancel + progress ---
        btn_row = QHBoxLayout()
        self.convert_btn = QPushButton("Convert Selected Folders")
        self.convert_btn.setStyleSheet(
            "background-color: #0052CC; color: white; font-weight: bold; "
            "padding: 12px; font-size: 12pt; border: none;")
        self.convert_btn.clicked.connect(self.convert_to_pdf)
        btn_row.addWidget(self.convert_btn, stretch=1)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setStyleSheet(
            "background-color: #FFFFFF; color: #B91C1C; font-weight: bold; "
            "padding: 12px; font-size: 12pt; border: 1px solid #FCA5A5;")
        self.cancel_btn.clicked.connect(self.cancel_conversion)
        self.cancel_btn.setVisible(False)
        btn_row.addWidget(self.cancel_btn)

        main_layout.addLayout(btn_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFormat("%p%")
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        main_layout.addWidget(self.progress_bar)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #6B7280;")
        self.status_label.setVisible(False)
        main_layout.addWidget(self.status_label)

        main_widget.setLayout(main_layout)
        self.setCentralWidget(main_widget)

    # ---- Folder selection ------------------------------------------------
    def browse_source_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Source Folder")
        if folder:
            self.source_folder = folder
            self.source_input.setText(folder)
            self.load_subfolders()

    def browse_output_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if folder:
            self.output_folder = folder
            self.output_input.setText(folder)

    def load_subfolders(self):
        """Scans in a background QThread so the window never freezes,
        even with hundreds of subfolders to check."""
        self.table.setRowCount(0)
        self.selected_folders = []
        if not os.path.exists(self.source_folder):
            return

        self.browse_source_btn.setEnabled(False)
        self.scan_status_label.setText("Scanning folders for images...")

        self.scan_worker = FolderScanWorker(self.source_folder)
        self.scan_worker.result_ready.connect(self.on_scan_ready)
        self.scan_worker.scan_error.connect(self.on_scan_error)
        self.scan_worker.start()

    def on_scan_ready(self, results):
        self.browse_source_btn.setEnabled(True)
        self.scan_status_label.setText(
            f"Found {len(results)} folder(s) with images." if results
            else "No subfolders with images found."
        )
        self._populate_table(results)

    def on_scan_error(self, message):
        self.browse_source_btn.setEnabled(True)
        self.scan_status_label.setText("")
        QMessageBox.warning(self, "Scan error", f"Could not scan source folder:\n{message}")

    def _populate_table(self, results):
        """Batch-insert all rows with UI updates suspended — inserting a
        QCheckBox + row one at a time with live updates is what made the
        table sluggish once folder counts got into the hundreds."""
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(results))
        for row, (folder_name, image_count) in enumerate(results):
            checkbox = QCheckBox()
            checkbox.setChecked(True)
            checkbox.stateChanged.connect(self.update_selected_folders)
            self.table.setCellWidget(row, 0, checkbox)
            self.table.setItem(row, 1, QTableWidgetItem(folder_name))
            self.table.setItem(row, 2, QTableWidgetItem(str(image_count)))
            self.table.setItem(row, 3, QTableWidgetItem("Ready"))
        self.table.setUpdatesEnabled(True)
        self.update_selected_folders()

    def update_selected_folders(self):
        self.selected_folders = []
        for row in range(self.table.rowCount()):
            checkbox = self.table.cellWidget(row, 0)
            if checkbox and checkbox.isChecked():
                folder_name = self.table.item(row, 1).text()
                folder_path = os.path.join(self.source_folder, folder_name)
                self.selected_folders.append(folder_path)

    def select_all(self):
        for row in range(self.table.rowCount()):
            self.table.cellWidget(row, 0).setChecked(True)

    def select_none(self):
        for row in range(self.table.rowCount()):
            self.table.cellWidget(row, 0).setChecked(False)

    def get_fitting_mode(self):
        if self.auto_fit.isChecked():
            return "auto"
        elif self.stretch_fit.isChecked():
            return "stretch"
        else:
            return "maintain"

    # ---- Conversion --------------------------------------------------
    def convert_to_pdf(self):
        if not self.output_folder:
            QMessageBox.warning(self, "Error", "Please select an output folder!")
            return

        if not self.selected_folders:
            QMessageBox.warning(self, "Error", "Please select at least one folder!")
            return

        generate_pdf = self.output_pdf_check.isChecked()
        generate_flipbook = self.output_flipbook_check.isChecked()

        if not generate_pdf and not generate_flipbook:
            QMessageBox.warning(self, "Error", "Please select at least one output format (PDF and/or Flipbook)!")
            return

        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.status_label.setVisible(True)
        self.status_label.setText(f"Starting conversion of {len(self.selected_folders)} folder(s)...")
        self.cancel_btn.setVisible(True)
        self._set_controls_enabled(False)

        fitting_mode = self.get_fitting_mode()

        self.worker = ConversionSupervisor(
            self.selected_folders, self.output_folder, fitting_mode,
            self.page_margin.currentText(), generate_pdf, generate_flipbook,
            self.processes_spin.value(),
        )
        self.worker.progress.connect(self.on_progress)
        self.worker.folder_done.connect(self.on_folder_done)
        self.worker.finished_all.connect(self.on_conversion_finished)
        self.worker.error.connect(self.on_conversion_error)
        self.worker.start()

    def cancel_conversion(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.status_label.setText("Cancelling — finishing in-progress folders...")
            self.cancel_btn.setEnabled(False)

    def on_progress(self, done, total):
        pct = int((done / total) * 100) if total else 0
        self.progress_bar.setValue(pct)
        self.status_label.setText(f"Converted {done} of {total} folders...")

    def on_folder_done(self, result):
        status = "Done ✓"
        if result['status'] == 'skip':
            status = "Skipped (no images)"
        elif result['status'] == 'error':
            status = f"Error: {result.get('error', 'unknown')}"
        self.update_folder_status(result['folder'], status)

    def _set_controls_enabled(self, enabled):
        self.convert_btn.setEnabled(enabled)
        self.select_all_btn.setEnabled(enabled)
        self.select_none_btn.setEnabled(enabled)
        self.output_pdf_check.setEnabled(enabled)
        self.output_flipbook_check.setEnabled(enabled)
        self.auto_fit.setEnabled(enabled)
        self.stretch_fit.setEnabled(enabled)
        self.maintain_fit.setEnabled(enabled)
        self.page_margin.setEnabled(enabled)
        self.processes_spin.setEnabled(enabled)
        self.browse_source_btn.setEnabled(enabled)
        for row in range(self.table.rowCount()):
            checkbox = self.table.cellWidget(row, 0)
            if checkbox:
                checkbox.setEnabled(enabled)
        if enabled:
            self.cancel_btn.setEnabled(True)

    def update_folder_status(self, folder_name, status):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 1)
            if item and item.text() == folder_name:
                self.table.setItem(row, 3, QTableWidgetItem(status))
                break

    def on_conversion_finished(self, summary):
        self.progress_bar.setVisible(False)
        self.status_label.setVisible(False)
        self.cancel_btn.setVisible(False)
        self._set_controls_enabled(True)

        if summary.get('cancelled'):
            QMessageBox.information(self, "Cancelled", "Conversion was cancelled.")
            return

        parts = []
        if self.output_pdf_check.isChecked():
            parts.append("PDF")
        if self.output_flipbook_check.isChecked():
            parts.append("HTML Flipbook")

        elapsed = summary['elapsed']
        QMessageBox.information(
            self, "Success",
            f"{' and '.join(parts)} conversion completed!\n\n"
            f"Successful: {summary['successful']}\n"
            f"Skipped: {summary['skipped']}\n"
            f"Failed: {summary['failed']}\n\n"
            f"Time: {elapsed:.1f} seconds ({elapsed/60:.1f} minutes)"
        )

    def on_conversion_error(self, error):
        self.progress_bar.setVisible(False)
        self.status_label.setVisible(False)
        self.cancel_btn.setVisible(False)
        self._set_controls_enabled(True)
        QMessageBox.critical(self, "Error", f"Conversion failed: {error}")


if __name__ == "__main__":
    # Required for multiprocessing to work correctly once this is packaged
    # into a Windows .exe with PyInstaller (see BUILD_PYQT5.bat).
    mp.freeze_support()
    app = QApplication(sys.argv)
    window = ImageToPDFApp()
    window.show()
    sys.exit(app.exec_())
