# CBRAIN GitHub 게시 안내

대상: CBRAIN Studio 2.1.0 | 작성일: 2026-09-16

이 안내는 공개 담당자가 **게시할 파일을 준비하고 GitHub에 올리는 순서**입니다. 실험 사용자는 [사용 매뉴얼](USER_MANUAL_KO.md)을 읽으세요. 이 문서를 만드는 작업에서는 GitHub 원격 저장소 생성이나 업로드를 실행하지 않습니다.

## A. 무엇을 올릴 것인가

소스와 작은 배포 펌웨어는 **Git 저장소**, 실행 `.app`은 **ZIP으로 GitHub Releases**에 올립니다. 현재 작업 폴더의 `app/`에는 여러 구버전이 함께 있으므로 폴더 전체를 그대로 업로드하지 않습니다.

| Git 저장소에 포함 | 이유 |
|---|---|
| `README.md`, `.gitignore`, `requirements-suite-lock.txt` | 시작점, 제외 규칙, Python 의존성 |
| `cbrain_studio/`, 선택된 `tools/`, `tests/` | 앱과 지원 모듈 소스, 빌드·검증 도구 |
| `firmware/cb_bridge/`, `firmware/cb_intan/`의 소스·설정 | 동글과 헤드스테이지 펌웨어 재현 |
| `app/firmware/`의 공통 HEX 2개와 manifest 2개 | 설치할 펌웨어와 빌드 당시 출처·해시 |
| `new_hardware/`의 핀맵과 EasyEDA JSON | 호환 보드와 연결 확인 |
| `docs/`, 매뉴얼 PDF, `app/START_HERE.md` | 설치·녹화·문제 해결 절차 |
| `release/CORE_FILES.txt` | 내보낼 파일을 명시한 목록 |

내보내기 도구가 추가하는 `release/PACKAGE_FILES.json`에는 실제 내보낸 파일의 SHA-256을 기록합니다. 이는 현재 패키지 검사용이며, 펌웨어 manifest 안의 빌드 당시 소스 기록과는 별개입니다.

**제외:** `.venv/`, 빌드·캐시 폴더, 구버전 앱/펌웨어, 실험 `.h5`, `recordings/`, `reports/`, Mac의 `devices.json`, UICR 백업, 개인 보정 HEX. Nordic SDK와 SEGGER 설치 파일은 저장소에 복사하지 않고 공식 배포처에서 설치합니다.

`.gitignore`는 실수 방지용이며 핵심 파일 선정 자체는 **CORE_FILES.txt**를 기준으로 합니다. 현재 Python 패키지에는 호환·시험용 과거 모듈도 남아 있습니다. 사용자 실행 진입점은 아래 네 파일이며, 예전 BLE/Pairer 도구를 배포 앱으로 다시 빌드하지 않습니다.

```text
tools/device_setup.py       -> CBRAIN Device Setup
tools/studio_gui.py         -> CBRAIN Studio
tools/fwbuilder_app.py      -> CBRAIN Firmware Builder
tools/recording_viewer.py    -> CBRAIN Recording Viewer
```

## B. 게시할 자료 만들기

다음 명령은 **프로젝트 루트**에서 실행합니다. GitHub에 전송하지 않고 새 폴더를 만듭니다. 기존 배포 앱을 다시 빌드하거나 기기에 펌웨어를 쓰지 않습니다.

GitHub는 일반 Git 파일의 100 MiB 초과를 차단하며, 브라우저 업로드 한도는 파일당 25 MiB입니다. 실행 앱은 작은 파일들을 포함하는 macOS 번들이므로 소스와 섞지 말고 아래의 보존형 ZIP을 Release asset으로 배포합니다. [GitHub 파일 크기 안내](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github).

```bash
python3 tools/prepare_github_release.py --apps
```

Python 3.10 이상과 macOS 기본 `ditto`, `codesign`을 사용합니다. `--apps`를 생략하면 소스 패키지만 만듭니다. 앱 묶음을 만들 때는 먼저 `app/`에 현재 버전 앱 4개가 있어야 합니다.

```text
build/github-ready/
  source/                                  # Git에 올릴 실제 폴더 내용
  CBRAIN-studio-2.1.0-source.zip             # 소스 배포 보관본
  CBRAIN-studio-2.1.0-macOS-arm64.zip        # 앱 4개 + 펌웨어 + 매뉴얼
  SHA256SUMS.txt                            # ZIP 파일 검증값
```

내보내기 도구는 기존 출력 폴더를 덮어쓰지 않습니다. 새로 만들려면 `--output build/github-ready-next`처럼 **새 출력 경로**를 지정합니다.

**확인 순서**

1. `source/`를 열어 네 앱의 진입점, 공통 HEX 2개, 매뉴얼과 화면 이미지가 있는지 확인합니다.
2. `.h5`, 실험 결과, 사용자 등록부, 구버전 앱이 들어 있지 않은지 확인합니다.
3. `SHA256SUMS.txt`와 ZIP을 함께 보관합니다. 아래 명령은 파일이 변하지 않았는지 검사합니다.
4. 다른 Mac에 배포하기 전 macOS ZIP을 새 위치에 풀고 Device Setup·Studio·Viewer가 실행되는지 확인합니다. 헤드스테이지 작업은 해당 Mac의 nrfjprog/J-Link 설치도 필요합니다.

```bash
cd build/github-ready
shasum -a 256 -c SHA256SUMS.txt
```

macOS ZIP은 `ditto`로 생성해 `.app`의 심볼릭 링크와 번들 구조를 보존하고, 생성 전에 앱 버전과 ad-hoc 코드 서명을 검사합니다. **Apple 공증(notarization)을 받은 배포판은 아닙니다.** 새 Mac의 첫 실행 절차는 사용 매뉴얼 2번을 따릅니다.

## C. GitHub에 소스와 Release 올리기

처음 게시할 때는 내용을 검토할 수 있도록 **Private 저장소**로 시작하고, 공개 범위와 라이선스는 저장소 소유자가 정합니다. 라이선스 파일이 없다면 임의로 MIT 등 다른 라이선스를 붙이지 않습니다.

**소스 업로드 - GitHub Desktop 사용**

1. GitHub에서 새 저장소를 만듭니다. 이름 예: `CBRAIN-studio`. 소유자와 공개 범위를 확인합니다.
2. [GitHub Desktop](https://desktop.github.com/)에 로그인하고 **File → Clone Repository**로 그 저장소를 새 로컬 폴더에 복제합니다.
3. B에서 만든 **`build/github-ready/source/` 안의 내용**을 복제된 저장소 폴더 안으로 복사합니다. `source` 폴더 자체를 한 단계 더 넣지 않습니다. Finder에서 `⌘⇧.`로 숨김 파일을 표시해 **.gitignore**도 포함합니다. 복제 폴더의 `.git`은 그대로 둡니다.
4. GitHub Desktop의 **Changes**에서 파일 목록을 검토합니다. 첫 화면의 README에서 매뉴얼 링크를 확인합니다. 녹화 데이터나 `.app` 폴더가 보이면 커밋 전에 제외합니다.
5. 요약에 `Add CBRAIN Studio 2.1.0`을 적고 현재 브랜치에 **Commit**한 뒤 **Push origin**을 누릅니다.
6. GitHub 웹에서 README, 사용 매뉴얼의 이미지와 내부 링크, 펌웨어 파일을 열어봅니다.

기존 저장소를 갱신할 때는 현재 브랜치·원격 주소를 확인하고 새 브랜치에서 변경을 검토합니다. 과거 파일이 이미 추적되고 있다면 `.gitignore`만 추가해도 자동으로 제거되는 것은 아닙니다. 연구 데이터가 올라간 이력이 있으면 별도로 이력을 검토합니다.

**실행 앱 배포 - GitHub Releases 사용**

1. 저장소의 **Releases → Draft a new release**를 엽니다.
2. 태그 예: `v2.1.0`. 방금 올린 소스 커밋을 대상으로 선택합니다. 이미 같은 태그가 있으면 덮어쓰지 말고 해당 릴리스 정책을 따릅니다.
3. 제목에 `CBRAIN Studio 2.1.0 - macOS Apple Silicon`을 넣습니다.
4. `CBRAIN-studio-2.1.0-macOS-arm64.zip`, `CBRAIN-studio-2.1.0-source.zip`, `SHA256SUMS.txt`를 첨부합니다. 매뉴얼 PDF도 바로 읽기 쉽게 별도 첨부할 수 있습니다.
5. 설명에 앱 4개, 동글 1.0.0-discovery, 헤드스테이지 0.3.0, nrfjprog/J-Link 의존성, 검증 범위를 적고 사용 매뉴얼 링크를 붙입니다.
6. 내려받는 사람의 경로로 ZIP을 다시 다운로드해 해시와 실행을 확인한 뒤 릴리스를 게시합니다. 검토 중이면 Draft로 보관합니다.

GitHub가 자동 생성하는 **Source code (zip)**에는 `.app`이 없습니다. 사용자가 실행 앱을 찾을 수 있도록 **macOS-arm64.zip을 받아야 한다**고 릴리스 첫 부분에 적습니다.

공식 도움말: [저장소 복제](https://docs.github.com/en/desktop/adding-and-cloning-repositories/cloning-and-forking-repositories-from-github-desktop), [릴리스 관리](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository).

## D. 개발자가 소스를 받아 실행·재현하기

배포 `.app`을 사용하는 실험 담당자는 이 단계가 필요 없습니다. 아래는 소스 저장소에서 개발하는 경우입니다.

1. Apple Silicon Mac에 Python 3.12를 준비합니다. 확인 환경은 Python 3.12.4입니다.
2. 저장소 루트에서 가상환경을 만들고 버전을 고정한 의존성을 설치합니다.
3. 네 진입점 중 필요한 앱을 실행합니다. Viewer는 하드웨어 없이 사용할 수 있습니다.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-suite-lock.txt
.venv/bin/python tools/recording_viewer.py
.venv/bin/python tools/device_setup.py
.venv/bin/python tools/studio_gui.py
.venv/bin/python tools/fwbuilder_app.py
```

각 실행 명령은 앱을 종료한 후 다음 명령을 실행합니다. 앱 재빌드는 아래 명령으로 하며 결과는 `dist_apps/`에 생깁니다. 빌드만으로 현재 `app/`의 배포본이 교체되지 않습니다.

```bash
.venv/bin/python tools/build_suite.py
```

펌웨어 빌드는 Nordic NCS **v3.4.0**과 해당 ARM 도구 모음이 별도로 필요합니다. `tools/build_headstage.sh`와 `tools/build_bridge_discovery.sh`의 `TC`와 `ZEPHYR_BASE`는 작성 Mac의 `/opt/nordic/ncs/` 설치 경로로 되어 있으므로 **자신의 SDK 설치 경로에 맞춰 확인**합니다. SDK나 툴체인 전체를 Git에 넣지 않습니다.

```bash
bash tools/build_bridge_discovery.sh
bash tools/build_headstage.sh
```

동글 빌드 결과는 `build/bridge_discovery/zephyr/zephyr.hex`입니다. 헤드스테이지 결과 `build/headstage_verified/zephyr/zephyr.hex`는 측정 설정 blob이 없는 원본입니다. **그 원본을 곧바로 배포용 headstage_common.hex로 바꾸지 않습니다.** 최초 사용자는 검증된 `app/firmware/`의 공통 HEX를 설치합니다. 펌웨어를 변경해 배포할 때는 설정 stamping, 주소·체크섬·ID 보존과 실물 검증 및 manifest 갱신까지 수행해야 합니다.

뷰어의 핵심 테스트는 다음과 같습니다. 전체 시험 구성과 검증 범위는 각 테스트 파일 및 [사용 매뉴얼 마지막 절](USER_MANUAL_KO.md)을 참고합니다.

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  tests/test_saved_recording.py tests/test_recording_viewer.py \
  -q -p no:cacheprovider
```

매뉴얼 화면은 `tools/manual_screenshots.py`, PDF는 `tools/make_manual_pdf.py`로 다시 만듭니다. PDF 재생성에는 별도 ReportLab과 한글 TrueType 글꼴이 필요하며 앱 실행 의존성에는 포함하지 않습니다.
