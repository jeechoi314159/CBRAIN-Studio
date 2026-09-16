# CB_INTAN headstage firmware 0.3.0

nRF52832 + RHD2216 전용입니다. UICR.CUSTOMER[0]의 번호로 `CBRAIN_N`을 광고하며,
nRF 동글을 통해 CB v2 DATA를 전송합니다. 현재 GUI는 Mac BLE를 사용하지 않습니다.

**0.3.0은 센서 초기화 실패 시 합성 파형으로 대체하던 동작을 제거했습니다.**
SPI 버스/전송, INTAN ROM, RHD2216 모델·채널 수, 설정 readback을 확인하고,
실패 시 원인 번호와 무효 데이터 표시를 전송합니다. LED 자동 판정도 중단합니다.
이 버전은 소프트웨어·빌드 검증을 마쳤으며, 현재 사용자의 보드에서 실물 검증하지 않았습니다.

- SPI: MOSI=P0.11, MISO=P0.12, SCLK=P0.13, CS=P0.14, mode 0, 1 MHz.
- RHD: 0.1–250 Hz, two's-complement, 0.195 µV/LSB.
- GATT: service 0xCB10, stream 0xCB11, control 0xCB12 (vendor UUID).
- 실험용 공통 HEX: `app/firmware/headstage_common.hex` — 4채널/1024 Hz, LED OFF.
- 이 파일에는 UICR 쓰기가 없습니다. Device Setup에서 기존 번호를 보존하며 설치합니다.

## 빌드와 설치

저장소 루트에서 `bash tools/build_headstage.sh`를 실행합니다. Nordic SDK를 변경하지 않고
저장소의 캐시 디렉터리와 `CMAKE_GDB=/usr/bin/true`를 사용합니다.
결과는 `build/headstage_verified/zephyr/zephyr.hex`입니다. 이 원본 HEX에는 측정 설정이
없습니다. 배포용 공통 파일에는 0x70000에 설정 blob을 넣으며, Builder는 새 공통 파일에
사용자 설정을 넣습니다. 설치는 Device Setup의 **기기 번호 · 펌웨어** 탭에서 진행합니다.

[원인, 사용자 설치 순서, 오류 코드, 검증 범위](../../docs/SENSOR_SOURCE.md)

## 검증

`tests/test_rhd_driver.py`는 실제 C 드라이버를 2-command 파이프라인의 모의 SPI 칩과
컴파일하여 정상 초기화·채널 순서·5개 오류 경로·읽기 중 전송 오류를 확인합니다.
`tests/test_sample_source.py`는 구형 시험 패턴, 무효 데이터 차단, 녹화 중 오류,
전압 환산과 파일 내보내기를 확인합니다. 이 테스트는 실물 아날로그 입력 시험을 대체하지 않습니다.

초기화 근거: [Intan RHD2000 데이터시트](https://intantech.com/files/Intan_RHD2000_series_datasheet.pdf).
이전 문서의 하드웨어 검증 주장은 현재 보드의 증거로 사용하지 않습니다.
