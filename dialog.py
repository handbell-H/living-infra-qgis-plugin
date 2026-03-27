import os
import re
import json

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QWidget,
    QLabel, QLineEdit, QPushButton, QFileDialog, QTextEdit,
    QListWidget, QDoubleSpinBox, QGroupBox, QAbstractItemView,
    QMessageBox, QFormLayout, QProgressDialog,
)
from qgis.PyQt.QtCore import QThread, pyqtSignal, QSettings, Qt
from qgis.PyQt.QtGui import QFont
from qgis.core import QgsProject, QgsVectorLayer

from .processing_core import (
    DEFAULT_VILLAGE, DEFAULT_HUB, KOR_NAME_MAP, FACILITY_MAP,
    step1_merge, step2_3_score, step4_aggregate,
    extract_facility_name,
)


# ── 미지 시설 영문명 입력 다이얼로그 ──────────────────────────
class MappingDialog(QDialog):
    def __init__(self, unknown_names, saved_map, parent=None):
        super().__init__(parent)
        self.setWindowTitle("시설 영문명 매핑")
        self.setMinimumWidth(440)
        self._edits = {}
        self._result = {}

        layout = QVBoxLayout()
        layout.addWidget(QLabel(
            "아래 시설의 영문 컬럼명을 입력하세요.\n"
            "(영문자로 시작, 영문/숫자/_ 조합, 최대 10자)"
        ))

        form = QFormLayout()
        for kor in unknown_names:
            edit = QLineEdit()
            edit.setMaxLength(10)
            edit.setPlaceholderText("예: newlib")
            if kor in saved_map:
                edit.setText(saved_map[kor])
            self._edits[kor] = edit
            form.addRow(QLabel(kor), edit)
        layout.addLayout(form)

        btn_row = QHBoxLayout()
        btn_ok = QPushButton("확인")
        btn_cancel = QPushButton("취소")
        btn_ok.clicked.connect(self._on_ok)
        btn_cancel.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(btn_ok)
        btn_row.addWidget(btn_cancel)
        layout.addLayout(btn_row)
        self.setLayout(layout)

    def _on_ok(self):
        pattern = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]{0,9}$')
        for kor, edit in self._edits.items():
            val = edit.text().strip()
            if not val:
                QMessageBox.warning(self, "입력 오류", f"'{kor}'의 영문명을 입력하세요.")
                return
            if not pattern.match(val):
                QMessageBox.warning(
                    self, "입력 오류",
                    f"'{val}'은 올바르지 않은 컬럼명입니다.\n"
                    "영문자로 시작하고 영문/숫자/_만 사용, 최대 10자."
                )
                return
        self._result = {kor: edit.text().strip() for kor, edit in self._edits.items()}
        self.accept()

    def get_mapping(self):
        return self._result


# ── 백그라운드 워커 ─────────────────────────────────────────
class Worker(QThread):
    log      = pyqtSignal(str)
    finished = pyqtSignal()
    error    = pyqtSignal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn     = fn
        self.args   = args
        self.kwargs = kwargs

    def run(self):
        try:
            self.fn(*self.args, log_fn=self.log.emit, **self.kwargs)
            self.finished.emit()
        except Exception as e:
            import traceback
            self.error.emit(traceback.format_exc())


# ── 메인 다이얼로그 ────────────────────────────────────────
class LivingInfraDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface       = iface
        self.worker      = None
        self.detected_map = {}   # kor -> eng for detected facilities
        self.settings    = QSettings('living_infra', 'mappings')

        self.setWindowTitle("국토생활인프라")
        self.setMinimumWidth(720)
        self.setMinimumHeight(640)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout()

        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab1(), "1단계: 합치기")
        self.tabs.addTab(self._tab2(), "2단계: 시설 분류")
        self.tabs.addTab(self._tab3(), "3단계: 점수 계산")
        self.tabs.addTab(self._tab4(), "4단계: 1km 집계")
        layout.addWidget(self.tabs)

        log_box = QGroupBox("로그")
        log_lay = QVBoxLayout()
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setFixedHeight(130)
        self.log_area.setFont(QFont("Consolas", 9))
        log_lay.addWidget(self.log_area)
        log_box.setLayout(log_lay)
        layout.addWidget(log_box)

        btn_close = QPushButton("닫기")
        btn_close.clicked.connect(self.close)
        layout.addWidget(btn_close)

        self.setLayout(layout)

    def _path_row(self, label, placeholder=""):
        row = QHBoxLayout()
        lbl = QLabel(label)
        lbl.setFixedWidth(130)
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        btn = QPushButton("찾아보기")
        btn.setFixedWidth(80)
        row.addWidget(lbl)
        row.addWidget(edit)
        row.addWidget(btn)
        return row, edit, btn

    # ── Tab 1 ──────────────────────────────────────────────
    def _tab1(self):
        w = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(10)

        row1, self.edit_access, btn1 = self._path_row("접근성 SHP 폴더")
        btn1.clicked.connect(lambda: self._browse_dir(self.edit_access))
        layout.addLayout(row1)

        row2, self.edit_output, btn2 = self._path_row("출력 폴더")
        btn2.clicked.connect(lambda: self._browse_dir(self.edit_output))
        layout.addLayout(row2)

        btn_scan = QPushButton("🔍  폴더 스캔 (시설 감지)")
        btn_scan.setMinimumHeight(32)
        btn_scan.clicked.connect(self._scan_folder)
        layout.addWidget(btn_scan)

        self.lbl_detected = QLabel("※ 폴더를 선택 후 스캔하세요.")
        self.lbl_detected.setWordWrap(True)
        layout.addWidget(self.lbl_detected)

        self.btn_run1 = QPushButton("▶  합치기 실행")
        self.btn_run1.setMinimumHeight(36)
        self.btn_run1.clicked.connect(self._run_step1)
        layout.addWidget(self.btn_run1)
        layout.addStretch()

        w.setLayout(layout)
        return w

    # ── Tab 2 ──────────────────────────────────────────────
    def _tab2(self):
        w = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(QLabel("시설을 선택 후 버튼으로 마을/거점 간 이동하세요."))

        lists_row = QHBoxLayout()

        vil_box = QGroupBox("마을시설")
        vil_lay = QVBoxLayout()
        self.list_village = QListWidget()
        self.list_village.setSelectionMode(QAbstractItemView.ExtendedSelection)
        vil_lay.addWidget(self.list_village)
        vil_box.setLayout(vil_lay)
        lists_row.addWidget(vil_box)

        btn_col = QVBoxLayout()
        btn_col.addStretch()
        btn_to_hub = QPushButton("→")
        btn_to_vil = QPushButton("←")
        btn_to_hub.setFixedWidth(40)
        btn_to_vil.setFixedWidth(40)
        btn_to_hub.clicked.connect(self._move_to_hub)
        btn_to_vil.clicked.connect(self._move_to_village)
        btn_col.addWidget(btn_to_hub)
        btn_col.addWidget(btn_to_vil)
        btn_col.addStretch()
        lists_row.addLayout(btn_col)

        hub_box = QGroupBox("거점시설")
        hub_lay = QVBoxLayout()
        self.list_hub = QListWidget()
        self.list_hub.setSelectionMode(QAbstractItemView.ExtendedSelection)
        hub_lay.addWidget(self.list_hub)
        hub_box.setLayout(hub_lay)
        lists_row.addWidget(hub_box)

        layout.addLayout(lists_row)

        self.btn_run2 = QPushButton("▶  2단계 완료")
        self.btn_run2.setMinimumHeight(36)
        self.btn_run2.clicked.connect(self._run_step2_done)
        layout.addWidget(self.btn_run2)

        btn_reset = QPushButton("분류 초기화")
        btn_reset.clicked.connect(self._reset_classification)
        layout.addWidget(btn_reset)

        w.setLayout(layout)
        return w

    # ── Tab 3 ──────────────────────────────────────────────
    def _tab3(self):
        w = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(10)

        thr_box = QGroupBox("거리 기준값")
        thr_lay = QVBoxLayout()

        row_v = QHBoxLayout()
        row_v.addWidget(QLabel("마을시설 기준 (km):"))
        self.spin_vil = QDoubleSpinBox()
        self.spin_vil.setRange(0.1, 50.0)
        self.spin_vil.setSingleStep(0.5)
        self.spin_vil.setValue(1.0)
        self.spin_vil.setFixedWidth(80)
        row_v.addWidget(self.spin_vil)
        row_v.addStretch()
        thr_lay.addLayout(row_v)

        row_h = QHBoxLayout()
        row_h.addWidget(QLabel("거점시설 기준 (km):"))
        self.spin_hub = QDoubleSpinBox()
        self.spin_hub.setRange(0.1, 50.0)
        self.spin_hub.setSingleStep(0.5)
        self.spin_hub.setValue(5.0)
        self.spin_hub.setFixedWidth(80)
        row_h.addWidget(self.spin_hub)
        row_h.addStretch()
        thr_lay.addLayout(row_h)

        thr_box.setLayout(thr_lay)
        layout.addWidget(thr_box)

        self.btn_run3 = QPushButton("▶  점수 계산 실행")
        self.btn_run3.setMinimumHeight(36)
        self.btn_run3.clicked.connect(self._run_step3)
        layout.addWidget(self.btn_run3)
        layout.addStretch()

        w.setLayout(layout)
        return w

    # ── Tab 4 ──────────────────────────────────────────────
    def _tab4(self):
        w = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(10)

        row1, self.edit_score, btn1 = self._path_row("점수 SHP (step3)")
        btn1.clicked.connect(lambda: self._browse_file(self.edit_score))
        self.edit_score.setPlaceholderText("비워두면 출력폴더의 step3_score.shp 사용")
        layout.addLayout(row1)

        row2, self.edit_grid, btn2 = self._path_row("1km 격자 SHP")
        btn2.clicked.connect(lambda: self._browse_file(self.edit_grid))
        layout.addLayout(row2)

        self.btn_run4 = QPushButton("▶  공간단위 집계 실행")
        self.btn_run4.setMinimumHeight(36)
        self.btn_run4.clicked.connect(self._run_step4)
        layout.addWidget(self.btn_run4)

        info = QLabel(
            "1km 격자가 아닌, 행정동·시군구·시도·10km 격자 등\n"
            "다양한 공간단위로 집계할 수 있습니다.\n"
            "단, 500m 격자 중심점 기준으로 집계합니다."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: gray; font-size: 10px; margin-top: 6px;")
        layout.addWidget(info)

        layout.addStretch()

        w.setLayout(layout)
        return w

    # ── 폴더 스캔 ──────────────────────────────────────────
    def _load_custom_mappings(self):
        raw = self.settings.value('custom_mappings', '{}')
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _save_custom_mappings(self, mapping):
        self.settings.setValue('custom_mappings', json.dumps(mapping, ensure_ascii=False))

    def _scan_folder(self):
        access_dir = self.edit_access.text().strip()
        if not access_dir or not os.path.isdir(access_dir):
            QMessageBox.warning(self, "경고", "접근성 SHP 폴더를 먼저 선택하세요.")
            return

        shp_files = sorted([f for f in os.listdir(access_dir) if f.endswith('.shp')])
        if not shp_files:
            QMessageBox.warning(self, "경고", "SHP 파일이 없습니다.")
            return

        saved_map = self._load_custom_mappings()
        detected = {}
        unknown_kor = []

        for shp_file in shp_files:
            kor_name = extract_facility_name(shp_file)
            if not kor_name:
                continue
            if kor_name in FACILITY_MAP:
                detected[kor_name] = FACILITY_MAP[kor_name]
            elif kor_name in saved_map:
                detected[kor_name] = saved_map[kor_name]
            else:
                unknown_kor.append(kor_name)

        if unknown_kor:
            dlg = MappingDialog(unknown_kor, saved_map, self)
            if dlg.exec_() == QDialog.Accepted:
                new_map = dlg.get_mapping()
                saved_map.update(new_map)
                self._save_custom_mappings(saved_map)
                detected.update(new_map)
            else:
                self._log("[경고] 알 수 없는 시설의 영문명 미입력. 해당 시설은 제외됩니다.")

        self.detected_map = detected
        names = ', '.join([f"{k}({v})" for k, v in detected.items()])
        self.lbl_detected.setText(f"감지된 시설 ({len(detected)}종): {names}")
        self._log(f"스캔 완료: {len(detected)}종 감지")
        self._update_classification_lists()

    def _update_classification_lists(self):
        self.list_village.clear()
        self.list_hub.clear()
        active_map = self._get_active_facility_map()

        for eng in DEFAULT_VILLAGE:
            kor = KOR_NAME_MAP.get(eng, eng)
            if kor in active_map or eng in active_map.values():
                self.list_village.addItem(f"{kor} ({eng})")

        for eng in DEFAULT_HUB:
            kor = KOR_NAME_MAP.get(eng, eng)
            if kor in active_map or eng in active_map.values():
                self.list_hub.addItem(f"{kor} ({eng})")

        if self.detected_map:
            known_engs = set(DEFAULT_VILLAGE + DEFAULT_HUB)
            for kor, eng in self.detected_map.items():
                if eng not in known_engs:
                    self.list_village.addItem(f"{kor} ({eng})")

    def _get_active_facility_map(self):
        if self.detected_map:
            return self.detected_map
        return FACILITY_MAP

    # ── 헬퍼 ───────────────────────────────────────────────
    def _browse_dir(self, edit):
        path = QFileDialog.getExistingDirectory(self, "폴더 선택")
        if path:
            edit.setText(path)

    def _browse_file(self, edit):
        path, _ = QFileDialog.getOpenFileName(self, "파일 선택", "", "SHP Files (*.shp)")
        if path:
            edit.setText(path)

    def _log(self, msg):
        self.log_area.append(msg)
        self.log_area.verticalScrollBar().setValue(
            self.log_area.verticalScrollBar().maximum()
        )

    def _set_buttons_enabled(self, enabled):
        for btn in [self.btn_run1, self.btn_run2, self.btn_run3, self.btn_run4]:
            btn.setEnabled(enabled)

    def _move_to_hub(self):
        for item in self.list_village.selectedItems():
            self.list_village.takeItem(self.list_village.row(item))
            self.list_hub.addItem(item.text())

    def _move_to_village(self):
        for item in self.list_hub.selectedItems():
            self.list_hub.takeItem(self.list_hub.row(item))
            self.list_village.addItem(item.text())

    def _reset_classification(self):
        merged_path = os.path.join(self.edit_output.text().strip(), 'step1_merged.shp')
        if os.path.exists(merged_path):
            self._update_tab2_from_merged(merged_path)
        else:
            self.list_village.clear()
            self.list_hub.clear()
            self.list_village.addItem("← 1단계(합치기)를 먼저 실행하세요.")

    def _get_eng_list(self, list_widget):
        result = []
        for i in range(list_widget.count()):
            text = list_widget.item(i).text()
            eng = text.split('(')[-1].rstrip(')')
            result.append(eng)
        return result

    def _load_to_qgis(self, shp_path, layer_name):
        if os.path.exists(shp_path):
            layer = QgsVectorLayer(shp_path, layer_name, 'ogr')
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
                self._log(f"  → QGIS 레이어 추가: {layer_name}")

    def _start_worker(self, fn, on_finished, *args, **kwargs):
        self._set_buttons_enabled(False)

        self._progress = QProgressDialog("처리 중...", None, 0, 0, self)
        self._progress.setWindowTitle("실행 중")
        self._progress.setWindowModality(Qt.WindowModal)
        self._progress.setMinimumWidth(300)
        self._progress.setCancelButton(None)
        self._progress.show()

        self.worker = Worker(fn, *args, **kwargs)
        self.worker.log.connect(self._log)
        self.worker.finished.connect(on_finished)
        self.worker.error.connect(self._on_error)
        self.worker.start()

    def _close_progress(self):
        if hasattr(self, '_progress') and self._progress:
            self._progress.close()
            self._progress = None

    # ── 실행 ───────────────────────────────────────────────
    def _run_step2_done(self):
        village = self._get_eng_list(self.list_village)
        hub     = self._get_eng_list(self.list_hub)
        if not village and not hub:
            QMessageBox.warning(self, "경고", "먼저 1단계를 실행하세요.")
            return
        self._log(f"\n=== 2단계 완료: 마을 {len(village)}종 / 거점 {len(hub)}종 ===")
        for eng in village:
            self._log(f"  [마을] {eng}")
        for eng in hub:
            self._log(f"  [거점] {eng}")
        QMessageBox.information(self, "완료", "2단계 완료!")
        self.tabs.setCurrentIndex(2)

    def _run_step1(self):
        access_dir = self.edit_access.text().strip()
        output_dir = self.edit_output.text().strip()
        if not access_dir or not os.path.isdir(access_dir):
            QMessageBox.warning(self, "경고", "접근성 SHP 폴더를 선택하세요.")
            return
        if not output_dir:
            QMessageBox.warning(self, "경고", "출력 폴더를 선택하세요.")
            return
        if not self.detected_map:
            self._log("자동 스캔 중...")
            self._scan_folder()
        active_map = self._get_active_facility_map()
        self._log("\n=== 1단계: SHP 합치기 ===")
        self._start_worker(step1_merge, self._on_step1_done,
                           access_dir, output_dir, active_map)

    def _on_step1_done(self):
        self._close_progress()
        merged_path = os.path.join(self.edit_output.text(), 'step1_merged.shp')
        self._load_to_qgis(merged_path, 'step1_merged')
        self._update_tab2_from_merged(merged_path)
        self._log("=== 1단계 완료 ===")
        self._set_buttons_enabled(True)
        QMessageBox.information(self, "완료", "1단계 완료!")
        self.tabs.setCurrentIndex(1)

    def _update_tab2_from_merged(self, merged_path):
        """step1_merged.shp 의 실제 컬럼을 읽어 Tab 2 리스트를 갱신."""
        if not os.path.exists(merged_path):
            return
        layer = QgsVectorLayer(merged_path, 'tmp_meta', 'ogr')
        if not layer.isValid():
            return

        META_FIELDS = {'gid', 'sgg_cd', 'sgg_nm_k', 'sido_cd', 'sido_nm_k'}
        merged_engs = [
            f.name() for f in layer.fields()
            if f.name() not in META_FIELDS
        ]

        # eng -> kor 역매핑 (기본 + detected)
        eng_to_kor = {v: k for k, v in FACILITY_MAP.items()}
        if self.detected_map:
            eng_to_kor.update({v: k for k, v in self.detected_map.items()})

        self.list_village.clear()
        self.list_hub.clear()

        hub_set = set(DEFAULT_HUB)
        for eng in merged_engs:
            kor = eng_to_kor.get(eng, eng)
            label = f"{kor} ({eng})"
            if eng in hub_set:
                self.list_hub.addItem(label)
            else:
                self.list_village.addItem(label)

        self._log(f"  Tab 2 갱신: 마을 {self.list_village.count()}종 / "
                  f"거점 {self.list_hub.count()}종")

    def _run_step3(self):
        output_dir  = self.edit_output.text().strip()
        merged_path = os.path.join(output_dir, 'step1_merged.shp')
        if not os.path.exists(merged_path):
            QMessageBox.warning(self, "경고", "먼저 1단계를 실행하세요.")
            return
        village = self._get_eng_list(self.list_village)
        hub     = self._get_eng_list(self.list_hub)
        vil_thr = self.spin_vil.value()
        hub_thr = self.spin_hub.value()
        self._log(f"\n=== 3단계: 점수 계산 (마을 ≤{vil_thr}km / 거점 ≤{hub_thr}km) ===")
        self._start_worker(
            step2_3_score, self._on_step3_done,
            merged_path, village, hub, vil_thr, hub_thr, output_dir
        )

    def _on_step3_done(self):
        self._close_progress()
        out_dir = self.edit_output.text()
        self._load_to_qgis(os.path.join(out_dir, 'step2_binary.shp'), 'step2_binary')
        self._load_to_qgis(os.path.join(out_dir, 'step3_score.shp'),  'step3_score')
        self._log("=== 3단계 완료 ===")
        self._set_buttons_enabled(True)
        QMessageBox.information(self, "완료", "3단계 완료!")
        self.tabs.setCurrentIndex(3)

    def _run_step4(self):
        # score SHP: 직접 지정 > 출력폴더 자동
        score_path = self.edit_score.text().strip()
        if not score_path:
            output_dir = self.edit_output.text().strip()
            score_path = os.path.join(output_dir, 'step3_score.shp')
        if not os.path.exists(score_path):
            QMessageBox.warning(self, "경고",
                "점수 SHP(step3_score.shp)를 찾을 수 없습니다.\n"
                "직접 파일을 선택하거나 출력 폴더를 확인하세요.")
            return
        grid_path = self.edit_grid.text().strip()
        if not os.path.exists(grid_path):
            QMessageBox.warning(self, "경고", "1km 격자 SHP 파일을 선택하세요.")
            return
        # 출력 폴더: score SHP와 같은 폴더 사용 (출력폴더 미지정 시)
        output_dir = self.edit_output.text().strip() or os.path.dirname(score_path)
        self._step4_output_dir = output_dir  # 완료 핸들러에서 사용
        self._log("\n=== 4단계: 공간단위 집계 ===")
        self._start_worker(
            step4_aggregate, self._on_step4_done,
            score_path, grid_path, output_dir
        )

    def _on_step4_done(self):
        self._close_progress()
        out_dir  = getattr(self, '_step4_output_dir', self.edit_output.text())
        shp_path = os.path.join(out_dir, 'step4_1km.shp')
        if os.path.exists(shp_path):
            layer = QgsVectorLayer(shp_path, 'step4_1km', 'ogr')
            if layer.isValid():
                style_path = os.path.join(
                    os.path.dirname(__file__), 'style_tot_avg.qml'
                )
                if os.path.exists(style_path):
                    msg, ok = layer.loadNamedStyle(style_path)
                    if ok:
                        layer.triggerRepaint()
                        self._log("  → 단계구분도 스타일 적용 (tot_avg)")
                    else:
                        self._log(f"  [경고] 스타일 적용 실패: {msg}")
                QgsProject.instance().addMapLayer(layer)
                self._log("  → QGIS 레이어 추가: step4_1km")
        self._log("=== 4단계 완료 ===")
        self._set_buttons_enabled(True)
        QMessageBox.information(self, "완료", "4단계 완료!")

    def _on_error(self, msg):
        self._close_progress()
        self._log(f"[오류]\n{msg}")
        self._set_buttons_enabled(True)
