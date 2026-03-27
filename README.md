1. 플러그인 개발 목적
국토지리정보원에서 제공하는 20종 생활인프라 시설별 접근성 자료(500m 폴리곤 SHP)를 통합하고, 마을·거점 시설 분류 및 거리 기준 적용을 통해 충족도 점수를 산출한다. 최종적으로 500m 격자 결과를 다양한 공간단위(1km 격자, 행정동, 시군구, 시도 등)로 집계하여 공간 분석 결과를 QGIS에서 바로 시각화할 수 있는 플러그인을 개발한다.

2. 입력 데이터 규격
2.1 시설 접근성 SHP
공간단위: 500m 격자 (폴리곤)
좌표계: EPSG:5179 (Korea 2000 Unified CS)
주요 컬럼: gid (격자 고유 ID), value (접근 거리, km), -999 = NoData
파일명 = 시설종류명 (예: 유치원.shp, 초등학교.shp)
총 20종 시설
2.2 집계 기준 SHP
1km 격자, 행정동, 시군구, 시도 등 사용자 지정 폴리곤
500m 격자 중심점이 포함되는 폴리곤 기준으로 집계

3. 분석 파이프라인
1단계: SHP 합치기
접근성 SHP 폴더 내 20개 파일을 gid 기준으로 조인하여 하나의 SHP로 통합한다. 파일명(한글)을 영문 컬럼명으로 자동 변환하며, 미지 시설은 MappingDialog를 통해 사용자가 직접 영문명을 입력한다. 입력 매핑은 QSettings에 저장되어 재사용된다.
출력: step1_merged.shp

2단계: 시설 분류
감지된 시설을 마을시설(기준 거리 ≤1km)과 거점시설(기준 거리 ≤5km)로 분류한다. 사용자가 드래그 UI로 시설을 자유롭게 이동할 수 있으며, 분류 리스트는 1단계 결과(step1_merged.shp)의 실제 컬럼 기준으로 자동 구성된다.
출력: (UI 설정)

3단계: 충족도 점수 계산
각 시설에 대해 접근 거리가 기준값 이하이면 1, 초과 또는 NoData(-999)이면 0으로 이진화한다. 마을시설 합계(vil_score), 거점시설 합계(hub_score), 전체 합계(tot_score)를 계산한다. 점수는 정수로 반올림하여 저장한다.
출력: step2_binary.shp / step3_score.shp

4단계: 공간단위 집계
500m 격자 폴리곤의 중심점을 계산하고, 집계 기준 SHP와 공간조인하여 1km 격자(또는 행정동·시군구 등) 단위로 min/max/avg를 집계한다. QgsSpatialIndex를 활용하여 공간조인 성능을 최적화하였다.
출력: step4_1km.shp

4. 주요 기술 결정 사항
▶ geopandas 제거 → PyQGIS native API
QGIS 내장 Python 환경에 geopandas가 설치되어 있지 않아 ModuleNotFoundError 발생. QgsVectorLayer, QgsFeature, QgsFields 등 PyQGIS 기본 API로 전면 재작성하여 외부 의존성 제거.

▶ Processing 알고리즘 제거 → Python 직접 반복
native:fieldcalculator를 시설 수(20종)만큼 반복 호출하면 알고리즘 오버헤드가 누적되어 속도 저하. 피처를 한 번 순회하며 모든 계산을 수행하는 방식으로 교체하여 처리 속도를 대폭 개선.

▶ QgsSpatialIndex 활용 공간조인
native:joinattributesbylocation 알고리즘 제거. 1km 격자에 공간 인덱스를 구축하고 bounding box로 후보를 추린 뒤 contains() 체크하여 대용량 데이터에서도 빠른 공간조인 수행.

▶ -999 NoData 처리
초기 구현에서 val <= threshold 조건이 -999에 대해 True를 반환하여 NoData를 충족으로 오판. (val >= 0) AND (val <= threshold) 조건으로 수정.

▶ ZIP 배포 방식
플러그인을 living_infra/ 폴더째 ZIP으로 패키징. QGIS 플러그인 관리자의 ZIP 설치 기능으로 외부 의존성 없이 설치 가능. ZIP 생성 시 version 필드를 증가시켜 변경 이력 관리.

▶ 단계구분도 스타일 자동 적용
QML 스타일 파일(style_tot_avg.qml)을 플러그인에 번들. 4단계 완료 후 loadNamedStyle() + triggerRepaint()로 tot_avg 필드에 5단계 단계구분도 자동 적용.

5. 플러그인 파일 구조
파일명
역할
__init__.py
classFactory 진입점
metadata.txt
플러그인 메타정보 (이름, 버전, 제작자)
living_infra.py
플러그인 등록, 메뉴/툴바 아이콘 연결
dialog.py
메인 UI (4탭 다이얼로그, Worker QThread, MappingDialog)
processing_core.py
핵심 분석 로직 (step1~step4)
icon.png
32×32 집 모양 아이콘 (Python으로 자동 생성)
style_tot_avg.qml
단계구분도 스타일 파일 (tot_avg 기준 5단계)

6. 설치 및 실행
QGIS 실행 → 플러그인 → 플러그인 관리 및 설치
ZIP에서 설치 → living_infra.zip 선택
툴바 또는 메뉴 [국토생활인프라] → 플러그인 실행
1단계: 접근성 SHP 폴더 및 출력 폴더 선택 → 폴더 스캔 → 합치기 실행
2단계: 마을/거점 시설 분류 확인 → 2단계 완료
3단계: 거리 기준값 입력 (마을 기본 1km, 거점 기본 5km) → 점수 계산 실행
4단계: 집계 기준 SHP 선택 → 공간단위 집계 실행 → 단계구분도 자동 표시

