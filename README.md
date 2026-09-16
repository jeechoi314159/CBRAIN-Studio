# CBRAIN Studio 2.1.0

nRF USB 동글을 통해 여러 헤드스테이지를 선택·연결·녹화하는 데스크톱 앱 모음입니다.
Mac Bluetooth는 사용하지 않습니다. 동글 하나당 헤드스테이지 하나를 연결합니다.

| 앱 | 사용하는 때 |
|---|---|
| **CBRAIN Device Setup** | ID 확인/부여, 헤드스테이지 펌웨어 설치, 동글 페어링 시험·조합 저장 |
| **CBRAIN Studio** | 한 개 또는 여러 기기 실험: 파형 확인, 원본 녹화, 저장 결과 검증 |
| **CBRAIN Firmware Builder** | 측정 조건/LED 규칙 설정, 신호 보정, 맞춤 HEX 생성 |
| **CBRAIN Recording Viewer** | 저장된 .h5 열기, 채널 선택·시간 이동·확대, 파형 PNG 저장 |

[처음부터 따라 하는 사용 매뉴얼](docs/USER_MANUAL_KO.md) · [인쇄용 PDF](output/pdf/CBRAIN_Manual_KO.pdf) · [GitHub 게시 안내](docs/GITHUB_PUBLISH_KO.md)

실행 앱은 **GitHub Releases의 macOS-arm64.zip**에서 받습니다. GitHub의 자동 생성 Source code ZIP에는 `.app`이 없습니다. 네 앱을 같은 `app/` 폴더에 두세요. 빠른 요약은 [시작 안내](app/START_HERE.md)를 참고하세요.

2.1.0: 저장 파일 전용 **Recording Viewer**를 추가했습니다. 동글 없이 실행하며,
실제 저장 파일을 읽기 전용으로 표시합니다. [뷰어 사용법](docs/RECORDING_VIEWER.md).

2.0.6: 작업 로그가 마지막 줄을 따라가며, 검색 화면에서 USB 동글 수·사용 중인 수와 점유 앱을 구분합니다.
다른 CBRAIN 앱이 동글을 사용 중이면 그 앱에서 연결을 해제한 뒤 검색하세요.

2.0.5: 헤드스테이지의 시험 신호를 실제 측정으로 표시하던 문제를 수정했습니다.
녹화에는 새 헤드스테이지 0.3.0 펌웨어가 필요합니다.
[원인과 설치 순서](docs/SENSOR_SOURCE.md)를 확인하세요.

## 실행

macOS에서는 `app/`의 네 `.app` 중 필요한 앱을 엽니다. 구형 Pairer/ID Assigner/Flasher는 새 Device Setup과
Studio로 통합했으며 `app/legacy-20260916/`에 보존했습니다.

소스로 실행하려면:

```bash
.venv/bin/python tools/device_setup.py
.venv/bin/python tools/studio_gui.py
.venv/bin/python tools/fwbuilder_app.py
.venv/bin/python tools/recording_viewer.py
```

macOS 앱 재빌드:

```bash
.venv/bin/python tools/build_suite.py
```

결과는 `dist_apps/`에 생성됩니다. 빌드 스크립트는 기존 `app/`을 자동 교체하거나 장치에
펌웨어를 설치하지 않습니다. PySide6/numpy/h5py/scipy/pyserial/PyInstaller가 필요합니다.

## 동글 펌웨어

검색 기능에는 `app/firmware/dongle_bridge.hex` 1.0.0-discovery가 필요합니다.
[설치·프로토콜 안내](docs/BRIDGE_DISCOVERY_PROTOCOL.md). 헤드스테이지 공통 펌웨어는
`app/firmware/headstage_common.hex`입니다.

## 설계와 검증

- [하드웨어 준비·설치·녹화와 검증 범위](docs/USER_MANUAL_KO.md)
- [배포 파일 구성과 개발 환경](docs/GITHUB_PUBLISH_KO.md)
- [저장 파일 뷰어](docs/RECORDING_VIEWER.md)

실물 RF/J-Link와 장시간 실험 검증은 별도입니다. 자동 테스트/앱 시작 성공은 실제 실험의
무손실 기록을 보장하는 결과가 아닙니다. 이전 문서는 docs/와 legacy 폴더에 보존했습니다.
