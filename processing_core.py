"""
PyQGIS 네이티브 기반 처리 모듈 - geopandas 불필요
무거운 공간 연산은 QGIS C++ processing 알고리즘 사용
"""
import os
import re

import processing as proc
from qgis.core import (
    QgsVectorLayer, QgsVectorFileWriter, QgsFields, QgsField,
    QgsFeature, QgsProject,
    QgsProcessingContext, QgsProcessingFeedback,
    QgsFeatureRequest,
)
from qgis.PyQt.QtCore import QVariant

FACILITY_MAP = {
    '유치원':          'kinder',
    '초등학교':        'elem',
    '작은도서관':      'smlib',
    '어린이집':        'daycare',
    '경로당':          'snrctr',
    '온종일 돌봄센터': 'allday',
    '의원':            'clinic',
    '약국':            'pharmacy',
    '생활권공원':      'lfpark',
    '버스정류장':      'busstp',
    '종합사회복지관':  'welfare',
    '노인여가복지시설':'snrleis',
    '공공체육시설':    'sports',
    '국공립도서관':    'publib',
    '공연문화시설':    'culture',
    '주제공원':        'thpark',
    '보건기관':        'health',
    '응급의료시설':    'emerg',
    '경찰서':          'police',
    '소방서':          'fire',
}
KOR_NAME_MAP    = {v: k for k, v in FACILITY_MAP.items()}
DEFAULT_VILLAGE = ['kinder', 'elem', 'smlib', 'daycare', 'snrctr',
                   'allday', 'clinic', 'pharmacy', 'lfpark', 'busstp']
DEFAULT_HUB     = ['welfare', 'snrleis', 'sports', 'publib', 'culture',
                   'thpark', 'health', 'emerg', 'police', 'fire']
FACILITY_ORDER  = DEFAULT_VILLAGE + DEFAULT_HUB


def extract_facility_name(filename):
    match = re.search(r'[\d.]+\s+(.+?)\(시군구격자\)', filename)
    return match.group(1).strip() if match else None


def _new_context():
    ctx = QgsProcessingContext()
    ctx.setProject(QgsProject.instance())
    return ctx, QgsProcessingFeedback()


def _write_to_shp(mem_layer, out_path, crs):
    try:
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = 'ESRI Shapefile'
        options.fileEncoding = 'UTF-8'
        err, msg, _, _ = QgsVectorFileWriter.writeAsVectorFormatV3(
            mem_layer, out_path,
            QgsProject.instance().transformContext(), options)
    except AttributeError:
        try:
            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = 'ESRI Shapefile'
            options.fileEncoding = 'UTF-8'
            err, msg = QgsVectorFileWriter.writeAsVectorFormatV2(
                mem_layer, out_path,
                QgsProject.instance().transformContext(), options)
        except AttributeError:
            err, msg = QgsVectorFileWriter.writeAsVectorFormat(
                mem_layer, out_path, 'UTF-8', crs, 'ESRI Shapefile')
    return err, msg


def _make_mem_layer(geom_type_str, crs, fields):
    layer = QgsVectorLayer(
        f"{geom_type_str}?crs={crs.authid()}", 'tmp', 'memory'
    )
    provider = layer.dataProvider()
    provider.addAttributes(fields)
    layer.updateFields()
    return layer, provider


# ── STEP 1: 20개 SHP 합치기 ────────────────────────────────
def step1_merge(access_dir, output_dir, fac_map=None, log_fn=print):
    active_map = fac_map if fac_map else FACILITY_MAP
    shp_files = sorted([f for f in os.listdir(access_dir) if f.endswith('.shp')])

    # ── 1) 각 SHP에서 gid→value 딕셔너리로 읽기 ──
    fac_data   = {}   # eng_name -> {gid: value}
    fac_ordered = []
    base_layer  = None
    crs         = None

    for shp_file in shp_files:
        kor_name = extract_facility_name(shp_file)
        if not kor_name or kor_name not in active_map:
            log_fn(f"[경고] '{kor_name}' 매핑 없음, 건너뜀")
            continue
        eng_name = active_map[kor_name]
        shp_path = os.path.join(access_dir, shp_file)
        lyr = QgsVectorLayer(shp_path, eng_name, 'ogr')
        if not lyr.isValid():
            log_fn(f"[경고] '{shp_file}' 로드 실패, 건너뜀")
            continue

        if base_layer is None:
            base_layer = lyr
            crs = lyr.crs()

        mapping = {}
        for feat in lyr.getFeatures():
            mapping[feat['gid']] = feat['value']
        fac_data[eng_name] = mapping
        fac_ordered.append(eng_name)
        log_fn(f"  {kor_name} ({eng_name}) 읽기 완료")

    if base_layer is None:
        log_fn("[오류] 처리할 SHP가 없습니다.")
        return

    # ── 2) 컬럼 순서 정렬 ──
    meta_names = ['gid', 'sgg_cd', 'sgg_nm_k', 'sido_cd', 'sido_nm_k']
    ordered_facs = [f for f in FACILITY_ORDER if f in fac_ordered]
    # FACILITY_ORDER에 없는 커스텀 시설은 뒤에 추가
    ordered_facs += [f for f in fac_ordered if f not in ordered_facs]

    # ── 3) 출력 필드 구성 ──
    out_fields = QgsFields()
    for fname in meta_names:
        idx = base_layer.fields().indexOf(fname)
        if idx >= 0:
            src = base_layer.fields().field(fname)
            out_fields.append(QgsField(src.name(), src.type(), '', src.length()))
    for eng in ordered_facs:
        out_fields.append(QgsField(eng, QVariant.Double, '', 10, 2))

    # ── 4) 피처 한 번 순회하며 조합 ──
    mem_layer, provider = _make_mem_layer('MultiPolygon', crs, out_fields)
    feats = []
    for feat in base_layer.getFeatures():
        gid = feat['gid']
        f = QgsFeature(out_fields)
        f.setGeometry(feat.geometry())
        attrs = [feat[fn] for fn in meta_names
                 if base_layer.fields().indexOf(fn) >= 0]
        attrs += [fac_data[eng].get(gid, None) for eng in ordered_facs]
        f.setAttributes(attrs)
        feats.append(f)
    provider.addFeatures(feats)

    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, 'step1_merged.shp')
    err, msg = _write_to_shp(mem_layer, out_path, crs)
    if err:
        log_fn(f"[오류] 저장 실패: {msg}")
    else:
        log_fn(f"저장: {out_path}  ({len(feats):,}행)")


# ── STEP 2+3: 이진화 + 충족도 점수 ────────────────────────
def step2_3_score(merged_path, village_facs, hub_facs,
                  vil_thr, hub_thr, output_dir, log_fn=print):
    layer = QgsVectorLayer(merged_path, 'merged', 'ogr')
    if not layer.isValid():
        log_fn("[오류] step1_merged.shp 로드 실패")
        return
    crs = layer.crs()

    fac_order  = village_facs + hub_facs
    thresholds = {f: vil_thr for f in village_facs}
    thresholds.update({f: hub_thr for f in hub_facs})
    active_facs = [f for f in fac_order if layer.fields().indexOf(f) >= 0]

    # ── binary 레이어 필드 구성 ──
    bin_fields = QgsFields()
    for field in layer.fields():
        bin_fields.append(QgsField(field.name(), field.type(), '', field.length()))
    for fac in active_facs:
        bin_fields.append(QgsField(f'{fac}_b', QVariant.Int, '', 2))

    bin_layer, bin_prov = _make_mem_layer('MultiPolygon', crs, bin_fields)

    # ── score 레이어 필드 구성 ──
    meta = ['gid', 'sgg_cd', 'sgg_nm_k', 'sido_cd', 'sido_nm_k']
    score_fields = QgsFields()
    for fname in meta:
        idx = layer.fields().indexOf(fname)
        if idx >= 0:
            src = layer.fields().field(fname)
            score_fields.append(QgsField(src.name(), src.type(), '', src.length()))
    score_fields.append(QgsField('vil_score', QVariant.Int, '', 3))
    score_fields.append(QgsField('hub_score', QVariant.Int, '', 3))
    score_fields.append(QgsField('tot_score', QVariant.Int, '', 3))

    score_layer, score_prov = _make_mem_layer('MultiPolygon', crs, score_fields)

    bin_feats   = []
    score_feats = []

    vil_set = set(village_facs)
    hub_set = set(hub_facs)

    for feat in layer.getFeatures():
        # binary 값 계산
        binary = {}
        for fac in active_facs:
            val = feat[fac]
            thr = thresholds[fac]
            binary[fac] = 1 if (val is not None and val >= 0 and val <= thr) else 0

        vil_score = sum(binary[f] for f in active_facs if f in vil_set)
        hub_score = sum(binary[f] for f in active_facs if f in hub_set)

        # binary 피처
        bf = QgsFeature(bin_fields)
        bf.setGeometry(feat.geometry())
        attrs = [feat[field.name()] for field in layer.fields()]
        attrs += [binary[fac] for fac in active_facs]
        bf.setAttributes(attrs)
        bin_feats.append(bf)

        # score 피처
        sf = QgsFeature(score_fields)
        sf.setGeometry(feat.geometry())
        s_attrs = [feat[fn] for fn in meta if layer.fields().indexOf(fn) >= 0]
        s_attrs += [vil_score, hub_score, vil_score + hub_score]
        sf.setAttributes(s_attrs)
        score_feats.append(sf)

    bin_prov.addFeatures(bin_feats)
    score_prov.addFeatures(score_feats)

    log_fn(f"  시설 {len(active_facs)}종 이진화 완료")

    out2 = os.path.join(output_dir, 'step2_binary.shp')
    _write_to_shp(bin_layer, out2, crs)
    log_fn(f"저장 (binary): {out2}")

    out3 = os.path.join(output_dir, 'step3_score.shp')
    _write_to_shp(score_layer, out3, crs)
    log_fn(f"저장 (score): {out3}  ({len(score_feats):,}행)")


# ── STEP 4: 500m → 1km 격자 집계 ──────────────────────────
def step4_aggregate(score_path, grid_1km_path, output_dir, log_fn=print):
    from qgis.core import QgsSpatialIndex

    # ── 1km 격자 공간 인덱스 구축 ──
    log_fn("  1km 격자 인덱스 구축 중...")
    grid_layer = QgsVectorLayer(grid_1km_path, 'grid', 'ogr')
    crs = grid_layer.crs()
    grid_meta = ['gid', 'sgg_cd', 'sgg_nm', 'sido_cd', 'sido_nm']

    grid_index = QgsSpatialIndex()
    grid_feats = {}   # fid -> feature
    for feat in grid_layer.getFeatures():
        grid_index.addFeature(feat)
        grid_feats[feat.id()] = feat
    log_fn(f"  1km 격자: {len(grid_feats):,}개")

    # ── 500m centroid → 1km gid 매핑 (Python 공간조인) ──
    log_fn("  spatial join 중...")
    score_layer = QgsVectorLayer(score_path, 'score', 'ogr')

    agg = {}   # gid_1km -> {vil, hub, tot}
    for feat in score_layer.getFeatures():
        centroid = feat.geometry().centroid().asPoint()
        candidates = grid_index.intersects(feat.geometry().boundingBox())
        for fid in candidates:
            gf = grid_feats[fid]
            if gf.geometry().contains(feat.geometry().centroid()):
                gid_1km = gf['gid']
                if gid_1km not in agg:
                    agg[gid_1km] = {'vil': [], 'hub': [], 'tot': []}
                agg[gid_1km]['vil'].append(int(feat['vil_score'] or 0))
                agg[gid_1km]['hub'].append(int(feat['hub_score'] or 0))
                agg[gid_1km]['tot'].append(int(feat['tot_score'] or 0))
                break
    log_fn(f"  join 완료: {sum(len(v['tot']) for v in agg.values()):,}개 매핑")

    out_fields = QgsFields()
    for fname in grid_meta:
        idx = grid_layer.fields().indexOf(fname)
        if idx >= 0:
            src = grid_layer.fields().field(fname)
            out_fields.append(QgsField(src.name(), src.type(), '', src.length()))
    for col in ['vil_min', 'vil_max', 'vil_avg',
                'hub_min', 'hub_max', 'hub_avg',
                'tot_min', 'tot_max', 'tot_avg']:
        out_fields.append(QgsField(col, QVariant.Int, '', 3))

    mem_layer, provider = _make_mem_layer('MultiPolygon', crs, out_fields)
    feats = []
    for feat in grid_layer.getFeatures():
        gid = feat['gid']
        if gid not in agg:
            continue
        d = agg[gid]
        vil, hub, tot = d['vil'], d['hub'], d['tot']
        f = QgsFeature(out_fields)
        f.setGeometry(feat.geometry())
        attrs = [feat[fn] for fn in grid_meta
                 if grid_layer.fields().indexOf(fn) >= 0]
        attrs += [
            min(vil), max(vil), round(sum(vil) / len(vil)),
            min(hub), max(hub), round(sum(hub) / len(hub)),
            min(tot), max(tot), round(sum(tot) / len(tot)),
        ]
        f.setAttributes(attrs)
        feats.append(f)

    provider.addFeatures(feats)
    out4 = os.path.join(output_dir, 'step4_1km.shp')
    err, msg = _write_to_shp(mem_layer, out4, crs)
    if err:
        log_fn(f"[오류] 저장 실패: {msg}")
    else:
        log_fn(f"저장: {out4}  ({len(feats):,}행)")
