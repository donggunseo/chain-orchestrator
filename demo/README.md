# CHAIN v0.4 데모

합성 Episode의 기록·검사를 공개하고, 엔진이 조회해 반영하는 과정을 확인한다.
의료진 결정은 별도의 결정 입력으로 전달한다.
주 State, 별도로 진행하는 작업, HITL 근거 개정과 타이머를 콘솔에서 확인할 수 있다.
실제 병원/API에 접속하거나 처방·투약하지 않는다. 알림과 EHR 표시는 `MOCK_RECORDED`다.

`python -m demo`는 현재 엔진의 합성 데모를 시작한다.
최소 범위는 응급실 등록부터 재관류 치료 시행 결정까지와 경로 종료 분기다.
입원·전원·EVT 전체를 시연하는 자료는 포함하지 않는다.

## State 식별자

콘솔과 조회 결과의 `state`는 Clinical의 `states`에 선언한 실행 ID다.
표시 이름은 각 State의 `State.name`에서 가져온다.

| 실행 ID | 최소 데모에서의 의미 |
|---|---|
| `ST-ED-010` | 응급실 등록·선별 대기 |
| `ST-ED-020` | 뇌졸중 의심 |
| `ST-IMG-010` | 영상검사 진행 결정 |
| `ST-IMG-020` | 해당 오더의 NCCT 완료 확인 |
| `ST-TX-010` | IVT 시행 결정 |
| `ST-EXIT-010` | 선별 음성 종료 |
| `ST-EXIT-020` | 뇌졸중 경로 아님 종료 |
| `S3N` | IVT 미시행 — 최소 데모 전용 임시 종료 |

`S3N`은 확장 임상 명세의 State가 아니다. 현재 NO 분기를 보존하는 용도로 사용하며,
해당 임상 분기가 구체화되면 변경한다. 이 데모의 실행 종료는 치료 완료나 환자 내원 종료를 뜻하지 않는다.
State ID를 변경한 구성은 새 Episode에 적용한다. 이미 시작한 실행은 원래 구성·Queue·ID를 유지한다.

## 준비와 실행

저장소 루트에서 Python 3.12와 기존 의존성을 준비한다.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Service 없이 새 엔진을 확인하는 Local 실행:

```sh
.venv/bin/python -m demo --backend local --test-mode \
  --test-timer-scale 0.1 --test-poll-interval-s 1 --run-timeout 1800 \
  --output-dir "output/fact-local-interactive-$(date +%Y%m%d-%H%M%S)"
```

### Temporal CLI 준비

`--start-local`을 사용하려면 설치된 Temporal CLI 실행 파일의 절대경로를 지정한다.
PATH에서 설치 경로가 나오는지 먼저 확인한다.

```sh
command -v temporal
```

경로가 출력되면 다음 명령으로 설정한다.

```sh
export CHAIN_TEMPORAL_SERVER_PATH="$(command -v temporal)"
```

출력이 없으면 이미 설치한 CLI의 절대경로를 직접 지정한다.
이 개발 컴퓨터에서 확인한 경로는 아래와 같다. 다른 컴퓨터의 공통 경로는 아니다.

```sh
export CHAIN_TEMPORAL_SERVER_PATH=/tmp/temporal-sdk-python-1.31.0
```

CLI 자체가 없으면 먼저 설치 파일을 준비한다. `/tmp`의 실행 파일은 임시 위치이므로 존재 여부를 다시 확인한다.
`/path/to/temporal` 같은 예시 경로를 그대로 입력하지 않는다. 설정한 뒤 같은 터미널에서 확인한다.

```sh
"$CHAIN_TEMPORAL_SERVER_PATH" --version
```

버전이 출력되면 아래 명령을 실행한다. 전체 설명은 [README의 CLI 경로 설정](../README.md#temporal-cli-경로-설정)을 따른다.

### 임시 Temporal Service 실행

위 CLI 준비가 끝난 터미널에서 실행한다.

```sh
.venv/bin/python -m demo --backend temporal --start-local --test-mode \
  --test-timer-scale 0.1 --test-poll-interval-s 1 --run-timeout 1800 \
  --output-dir "output/fact-temporal-interactive-$(date +%Y%m%d-%H%M%S)"
```

`--start-local`은 설치된 Temporal CLI로 임시 in-memory Service와 Worker를 함께 시작한다.
CLI를 다운로드하지 않으며 운영용 영속 Service 배포가 아니다.
`FileNotFoundError: Set CHAIN_TEMPORAL_SERVER_PATH ...`는 지정한 CLI를 찾지 못했다는 뜻이다.
이 단계에서 실패하면 Workflow가 시작되지 않아 `state=None`으로 기록된다.

### 기존 Temporal Service 연결

Service가 이미 실행 중이면 아래 명령으로 연결한다. CLI와 `CHAIN_TEMPORAL_SERVER_PATH`는 필요 없다.
`localhost:7233`은 로컬 예시이며 외부 Service는 실제 주소로 바꾼다.

```sh
.venv/bin/python -m demo --backend temporal --address localhost:7233 --test-mode \
  --test-timer-scale 0.1 --test-poll-interval-s 1 --run-timeout 1800 \
  --output-dir "output/fact-temporal-existing-$(date +%Y%m%d-%H%M%S)"
```

`--start-local`과 `--address`는 함께 사용하지 않는다.
기존 Service 연결은 기본 `default` namespace를 사용하며 namespace·TLS·API key 옵션은 없다.
두 방식 모두 데모 전용 Worker를 시작한다. 별도 Worker와 같은 Queue를 함께 처리하지 않는다.
실제 백엔드/Worker는 SDK로 Service에 연결하며 CLI 경로를 사용하지 않는다.
운영 namespace·인증 설정은 [README의 Service 연결 설정](../README.md#temporal-service-연결-설정)을 따른다.

### 실행 조건

출력 디렉터리는 실행마다 새 경로를 사용한다. 존재하는 디렉터리는 덮어쓰지 않는다.
원본 Episode의 도착시각과 상대시간을 사용하는 고정 합성 fixture이므로 `--test-mode`가 필요하다.
위 대화형 예제는 사람이 근거를 확인하고 입력할 시간과 자료 반영 대기시간을 따로 설정한다.

| 설정 | 대화형 예제 | 적용 범위 |
|---|---|---|
| `--test-timer-scale` | `0.1` | Clinical의 보류·재알림 타이머를 1/10로 축소. 10분 → 1분 |
| `--test-poll-interval-s` | `1` | 원천자료 조회 주기 종료 후 다음 조회까지 1초 대기 |
| `--run-timeout` | `1800` | 데모 전체 실행 제한 30분 |

기존 `--test-timer-scale 0.01`은 10분을 6초로 줄이므로 사람이 조작하기에는 빠르다.
더 여유 있게 확인하려면 `--test-timer-scale 1`로 원래 임상 시간을 사용한다.
Clinical 사본과 Deployment 사본에만 적용하며 원본 YAML이나 진행 중 실행의 설정을 바꾸지 않는다.
HITL 지연 타이머는 알림만 발생시키며 자동 승인·거부나 질문 만료가 아니다.
DEFER/HOLD 타이머는 재질문을 요청하고 `ready`로 먼저 재개할 수도 있다.

원본 자료 조회 간격은 30초다. 위 대화형 예제에서는 테스트 옵션으로 1초로 줄인다.
공개된 자료가 화면에 반영되기까지 조회·Activity·Agent/근거 준비 등의 처리 시간은 추가될 수 있다.
가상 병원이 자료를 공개한 즉시 Signal을 보내지는 않는다.
Agent·HITL 등의 준비 직전에도 같은 백엔드 client로 다시 조회한다.
운영 기본값을 바꾸려면 사용하는 Deployment YAML의 `reconciliation.poll_interval_s`를 수정한다.
정확한 파일과 적용 범위는 [README의 주기 설정](../README.md#원천자료-조회와-주기-설정)을 따른다.

짧은 자동 시험에는 `--test-mode --test-poll-interval-s 0.2`를 명시할 수 있다.
이 옵션은 이번 실행의 Deployment 사본에만 적용하며 기본 30초와 원본 파일을 변경하지 않는다.
`--test-timer-scale`은 Clinical 타이머에만 적용하므로 자료 조회 간격은 위 옵션으로 별도 지정한다.

병원의 가상 시계는 `TEST_CLOCK_AT`으로 표시한다. 엔진의 적용시각과 Timer는 실제 시계를 사용한다.
State/결정이 실제 수락된 것을 관측한 뒤 가상 시계의 현재 위치를 상대시간 기준점으로 잡고,
원래 엔진 처리시각도 따로 보존한다. 가상 시계 전진만으로 Timer가 완료됐다고 표시하지 않는다.

### 타이머 화면

일반 터미널에서 실행하면 타이머는 화면 하단의 한 줄에서 갱신된다.
여러 타이머는 같은 줄에 함께 표시하고, 그 위의 영역에서 로그와 HITL 입력을 처리한다.
카운트다운을 갱신하려고 입력 줄을 다시 출력하거나 입력 버퍼를 읽지 않는다.
상태줄은 화면 폭에 맞춰 줄이며, 같은 내용은 다시 그리지 않는다.

실제 만료·취소 등의 상태 변경은 기존 로그로 기록한다. 0초 표시는 만료 처리 대기이며
실제 Timer 완료를 대신하지 않는다. 조회가 지연되면 남은 시간을 확정하지 않고 확인 지연으로 표시한다.
타이머가 모두 종료되면 상태줄을 비우고, 종료·EOF·중단 시 기본 스크롤 영역을 복원한다.

표준 입출력이 터미널이 아닌 IDE 실행창·파일·파이프 또는 `TERM=dumb`에서는 제어문자를 쓰지 않고
일반 로그 형식을 사용한다. 실행 중 창 크기를 바꾸면 입력 줄이 재배치될 수 있으므로
해당 실행의 상태줄 갱신을 중단하고 기본 스크롤 영역으로 돌아간다. 이때도 엔진·Event 처리와 타이머는 계속된다.
이 표시 방식은 엔진·조회 주기·임상 타이머를 바꾸지 않는다.

## 화면을 따라 진행하기

1. 초기에는 식별 정보·나이·성별·병상·도착시각만 있다. 간호/초진 자료가 공개되면 Screening을 실행한다.
2. HITL #1이 열려도 독립적인 CBC/COAG 결과는 계속 유입된다.
   `DEMO_DECISION_READY | HITL_1_PROCEED_TO_CT`가 보이면 기본 시나리오의 응답 시점에 도달한 것이다.
3. PROCEED_TO_CT 결정이 엔진에 수락되고 `ST-IMG-010`에 들어온 뒤 가상 의료진이 OCS 오더를 입력한다.
   NCCT는 그 오더를 기준으로 도착한다. CHAIN의 자동 오더가 아니다.
4. 해당 NCCT와 오더가 일치하면 `ST-IMG-020`을 표시하고 tPA final 가상 결과·근거 준비 후 HITL #2를 연다.
5. YES는 `ST-TX-010`, NO는 `S3N`으로 끝난다. YES의 종료 지점은 시행 결정이며 실제 치료 완료가 아니다.

선택 코드/번호, 모의 역할·직원 ID·사유와 요구되는 확인사항을 입력한다.
HITL #1의 NOT_STROKE_PATHWAY는 `ST-EXIT-020`, Screening NEGATIVE는 `ST-EXIT-010`으로 끝난다.
DEFER/HOLD는 같은 State에 머무르며 타이머로 다시 질문한다.
보류 화면의 `ready`는 기다리는 도중 먼저 재요청하는 기능이다. 새 결정과 구분해 전송한다.

| 명령 | 동작 |
|---|---|
| `docs` | 현재 근거 개정이 참조한 정확한 버전의 원문 |
| `json` | 현재 질문·판단 Snapshot·선행 결과·직전 확인·별도 출처 원문 |
| `docs 1`, `json 1` | 같은 요청의 지정한 과거 근거 개정. 참고 열람이며 결정은 최신 개정에만 응답 |
| 보류 중 `ready` | 역할/ID를 입력하고 백엔드와 같은 native 재개 계약 전송 |
| `q` 또는 EOF | 결정을 만들지 않고 콘솔 종료. Workflow cancel/terminate 요청은 전송하지 않음 |

관련 자료가 갱신되면 같은 질문 ID의 근거 개정이 바뀐다. 입력 중 개정되면 이전 입력을 폐기하고
최신 근거를 확인한 뒤 다시 응답한다. 관련 없는 자료는 해당 질문을 바꾸지 않는다.
응답 전송 성공과 엔진의 수락/거부는 별도 표시한다.
`docs/json`은 선택한 개정의 자료만 보이고 최신 원문을 과거 개정에 섞지 않는다.

미확보·최신 여부 미확인·충돌은 그대로 표시한다. 원본 NIHSS 자료는 출처별 단위 표기가 달라
현재 `CONFLICT`로 남는다. 항응고제의 원본 `NO_EVIDENCE`도 확인된 음성으로 바꾸지 않고 `UNKNOWN`으로 남는다.
진행 중 Agent의 이전 입력 결과는 참고용이며, 완료 후 최신 입력으로 재실행한다.
Summary는 확보된 구조화 자료를 요청 범위로 조합하는 Mock이며 NLP 구현이 아니다.

## 기록된 응답으로 전체 연결 확인

자동 응답은 다음 명시적 테스트 옵션에서만 사용한다.
아래 `--start-local` 명령은 위 CLI 경로 설정을 완료한 같은 터미널에서 실행한다.

```sh
.venv/bin/python -m demo --backend temporal --start-local --test-mode \
  --hitl recorded --recorded demo/scenarios/recorded_hitl.json \
  --test-poll-interval-s 0.2 \
  --expected demo/scenarios/expected.json --run-timeout 180 \
  --output-dir "output/fact-temporal-recorded-$(date +%Y%m%d-%H%M%S)"
```

Local은 `--backend local`로 바꾸고 `--start-local`을 뺀다.
기존 Service는 `--start-local` 대신 `--address localhost:7233`을 사용하며 CLI 경로가 필요 없다.
완료 시 `RUN_OUTCOME | COMPLETED | state=ST-TX-010`과 종료코드 0을 남긴다.
기대 결과는 실행이 끝난 뒤에만 비교하고 Workflow에 전달하지 않는다.
사용 가능한 합성 응답/기대 자료:

| 시연 | 응답 | 기대 결과 |
|---|---|---|
| 기본 YES | `recorded_hitl.json` | `expected.json` |
| HITL #1 경로 종료 | `recorded_s1x.json` | `expected_s1x.json` |
| HITL #2 NO | `recorded_s3n.json` | `expected_s3n.json` |
| DEFER→PROCEED→HOLD→YES | `recorded_defer_hold.json` | `expected.json`; 짧은 재질문을 위해 `--test-timer-scale 0.01` |

파일은 모두 `demo/scenarios/` 아래다. NEGATIVE는 별도 고정 fixture 프로필
`--fixture-profile negative`로 실행한다. 이 경우 콘솔 요청 없이 `ST-EXIT-010`으로 종료하며
`--expected demo/scenarios/expected_s0x.json`으로 확인할 수 있다.
fixture에 없는 입력은 `FIXTURE_NOT_FOUND`로 명시 실패한다. 실행 중 정답을 생성하거나 PASS로 대체하지 않는다.

## 출력 자료

| 파일 | 내용 |
|---|---|
| `manifest.json`, `definition.json` | 실행/설정/설치/데모 코드 pin, 실제 공개 정의, 합성 모드와 가속 설정 |
| `clinical-effective.yaml` | 시간을 가속한 경우 이번 실행에 고정한 Clinical 사본 |
| `deployment-effective.yaml` | 테스트용 조회 간격을 명시한 경우 이번 실행에 고정한 Deployment 사본 |
| `snapshot.json` | 현재 State·마일스톤·작업·HITL 근거 개정·수락 결정·처리/확인 이력 |
| `progress.json`, `snapshot-status.json` | 마지막 경량 진행 정보와 전체 Snapshot 확보 여부·조회 진단. 전체 조회가 실패하면 `snapshot.json`을 경량 자료로 대체하지 않음 |
| `query-diagnostics.json`, `close-diagnostics.json` | 진행 조회의 실패/재시도/회복·소진, 종료 Snapshot/History/상태/Replay/정리의 단계별 결과 |
| `commands.jsonl` | 정확 Activity 요청·결과·오류·시도. 합성 자료를 포함 |
| `simulation.json`, `publications.json`, `source_publications.json` | 인과 기준점·원본/변환 해시·공개 사건·가상 의료진 오더 |
| `source_queries.json` | 직전 확인/독립 보정·정확 조회와 범위·호출 시각 |
| `backend_queries.json` | 주기적 조회와 작업 준비가 공통 `fetch_source`로 전달한 요청·목적·자료 범위 |
| `outcome.json`, `comparison.json` | 완료/실패/부분 종료와 사후 비교 |
| `temporal_history.json`, `temporal_execution.json`, `replay.json` | Temporal 실행에서 실제 History·상태·저장 History Replay 결과 |

Local 결과는 실제 Service 시험이 아니다. Temporal 결과도 합성 API/Agent/Mock의 연결 확인이다.
코드 2는 입력 종료·시간 초과 등 부분 결과, 코드 1은 실패/사후 비교 실패를 나타낸다.
`q` 후 기존 Service의 Workflow는 남지만 embedded Worker는 정리된다.
임시 in-memory Service를 사용했다면 데모 종료 때 Service와 메모리 History도 사라지므로 저장 파일로 확인한다.

## 모듈 연결 위치

| 작업 | 확인할 코드/계약 |
|---|---|
| 콘솔 없는 Episode 생성 | `chain.client.start_episode(client, configuration, initial, engine_run_id=...)` |
| 병원 자료 공개·발견 | `hospital.py`는 공개만 수행. `backend.py`의 `fetch_source`로 발견하고 엔진의 기존 Event Ingress로 반영 |
| 수동 Event | `submit_event({source_id, event})`. 별도 수동 입력도 동일한 검증·정확 버전 조회 경로 사용 |
| Agent·출처·알림 구현 | `providers.py`의 `provider_factory(binding, services)`와 `../config/minimum/installed.json`의 승인 entrypoint/bytes |
| 원천 API 조회 | `chain/backend_source.py`의 공통 Provider와 `fetch_source(request)`·`resolve_exact(event)` client 계약. 데모 client는 `backend.py` |
| HITL 표시/응답 | `console.py`의 native Request/개정 binding. `progress`로 개정 감시, `hitl_view(request_id, revision)`로 폼·원문 조회, receipt로 수락 확인 |
| 작업 계획 | `../config/minimum/clinical.yaml`의 사실/마일스톤/State 투영·작업/결정/타이머. Site는 출처·대상, Safety는 승인, Deployment는 구현·기술 제한 |

백엔드의 Signal·Query 계약은 콘솔 키 입력과 분리되어 있다. HTTP/Push·사용자 인증·실제 병원 API·운영 DB는 제공하지 않는다.
화면의 반복 조회는 원문/Agent 본문을 제외한 `progress`를 사용하고 동시 요청을 100ms 안에서 공유한다.
질문/개정이 바뀔 때 폼용 상세를 한 번 받고 `docs/json`은 지정 개정의 상세를 조회한다.
이 공유는 Agent/HITL 실행 준비의 원천 재확인을 생략하지 않는다.
직전 확인 실패는 현재 자료와 `UNCONFIRMED`로 진행하며 필수 Agent 성공까지 대신 만들지 않는다.
전체 진단 `snapshot`과 종료 결과는 유지하며 원문/결과 일부는 History에도 남는다.
제품 통합 시 장기 History·자료 보존 범위를 별도로 설계해야 한다.
조회 실패는 구간·오류 코드로 구분하고 최초 실행 오류와 Snapshot/사후 비교의 보조 진단을 별도로 남긴다.
진행 조회의 알려진 일시 오류는 최대3회, 회당5초·간격0.1초로 재시도한다.
이전 화면을 최신 자료로 대신 반환하지 않으며, 소진·영구 오류·Workflow 실패는 명시적으로 종료한다.
정상 완료 결과가 조회보다 먼저 도착하면 그 결과로 화면을 마무리한다.
Snapshot 조회 실패와 별개로 History·실행 상태·Replay를 수집하고 최초 오류를 보존한다.
내부 실행권 반환 Activity는 Temporal History에 기록되며 `commands.jsonl`의 임상/원천 작업과 구분한다.

고정 fixture의 생성 근거는 `provenance.json`, 승인 모듈의 파일 hash는 `config/minimum/installed*.json`에 있다.
런타임에서는 fixture를 새로 생성하거나 미공개 원천자료를 읽지 않는다.
