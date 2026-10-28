# 사실 기반 실행의 버전 보존과 운영 연결

이 안내는 `ChainFactEpisodeV1`의 실행 묶음과 Worker를 다룬다.
사용자·접수 API·인증·운영 저장소·병원 API는 통합 환경에서 제공한다.
새 실행 경로가 존재하는 것과 실제 신규 접수를 그 경로로 전환한 것은 구분한다.
현재 제품 버전은 0.4.0이며 `chain/`과 `python -m demo`를 사용한다. 제품 버전과 Event·Activity 계약 버전은 별도로 관리한다.

## Temporal Service 연결 환경

Temporal CLI 실행 파일과 Temporal Service의 연결 주소를 구분한다.

| 실행 방식 | 필요한 환경 |
|---|---|
| Local 데모 | Python과 의존성. Service·CLI 경로 불필요 |
| `demo --backend temporal --start-local` | 실행 가능한 Temporal CLI와 `CHAIN_TEMPORAL_SERVER_PATH` 경로 설정. 데모가 임시 Service·Worker 시작 |
| `demo --backend temporal --address ...` | 이미 동작하는 Service의 주소. CLI 불필요. 현재 `default` namespace 사용, TLS/API key 옵션 미제공 |
| 실제 백엔드·통합 Worker | 별도 운영 Service의 주소·namespace·필요한 인증, 같은 구성의 Queue·승인 모듈·private 서비스 |

`integration.worker`와 `chain.worker`는 Service를 시작하지 않고 `Client.connect()`로 접속한다.
`CHAIN_TEMPORAL_SERVER_PATH`를 읽지 않는다. Service 자체의 배포·기동·영속 저장소는 운영 환경에서 제공한다.
Worker CLI의 `--address`와 `--namespace`를 백엔드의 Client 설정과 맞춘다.
현재 CLI에는 TLS/API key 옵션이 없으므로 보안 연결은 애플리케이션과 Worker bootstrap에서 SDK 옵션으로 연결한다.
자격정보는 임상 YAML·Workflow 입력·Artifact에 적지 않는다.

데모의 임시 in-memory Service는 운영 영속 Service를 대신하지 않는다.
CLI 파일 경로는 설치된 환경마다 다르며 `/tmp/temporal-sdk-python-1.31.0`이 모든 컴퓨터에 있다고 가정하지 않는다.
CLI 설정/오류 해결은 [README](../../README.md#temporal-cli-경로-설정),
백엔드 연결은 [연동 안내](fact_integration.md#실행-연결)를 따른다.

## 실행마다 고정하는 값

`load_configuration()`은 Clinical/Site/Safety/Deployment·모듈 메타데이터·승인 설치 목록을 읽는다.
검증·컴파일 결과의 `configuration_hash`에 실행 정의, private Worker binding, 설치 증거와 구현 pin이 연결된다.
`build_start()`는 초기 입력과 실행 ID를 검증하고 해당 구성의 공개 정의·manifest를 Workflow에 전달한다.
그 뒤 YAML을 바꿔도 이미 시작한 실행의 값은 바뀌지 않는다.

| 대상 | 식별·보존 방법 |
|---|---|
| Workflow | Type `ChainFactEpisodeV1`; `engine_run_id`가 Temporal Workflow ID |
| Worker Queue | `chain-fact-v1-<configuration_hash의 앞 16자리>`; `queue_for_configuration()`으로 계산 |
| 실행 정의 | `public_definition`, `manifest`; Workflow 입력과 함께 고정 |
| Worker 연결 | `worker`, `artifact_evidence`; 승인 descriptor·파일 해시·출처 시스템 설정 |
| 실행 코드 | `chain/` 전체 구현·공통 계약 pin과 선택 모듈 bytes |
| 데이터 | `initial`, Event/완료, Snapshot, History, Mock receipt; 코드 묶음과 별도 보존 |

Configuration manifest의 `runtime_ready=false`와 초기 blocker 표기는 v1 설정 검사 형식의 당시 필드다.
이 필드를 현재 실행 가능 판정에 쓰지 않는다. 현재 CLI는 `scope: CONFIGURATION_INSPECTION`을 반환한다.
실제 실행에서는 `build_start()`와 `FactWorkerRuntime`이 native 계약·설치·pin·미지원 수명을 검사한다.
`--validation-only`로 만든 미설치 구성은 실행용이 아니다. 구버전 YAML·Registry 연결은 현재 로더가 거부한다.

## Worker Artifact 생성

현재 실행 코드를 재현할 수 있도록 설정과 승인 파일을 새 private 디렉터리로 묶는다.
저장소 루트에서 다음을 실행한다. 같은 출력 경로는 재사용하지 않는다.

```sh
.venv/bin/python tools/freeze_fact_artifact.py \
  --clinical config/minimum/clinical.yaml \
  --site config/minimum/site.yaml \
  --safety config/minimum/safety.yaml \
  --deployment config/minimum/deployment.yaml \
  --metadata config/minimum/module_metadata.json \
  --installed config/minimum/installed.json \
  --installation-root . --output-dir output/frozen-fact-worker
```

이 명령은 Service를 시작하거나 파일을 업로드하지 않는다. 접수 API·기존 Worker·Queue를 변경하지 않는다.
예시는 합성 모듈을 선택하므로 운영용 병원/Agent 구현이 되는 것은 아니다.

| 생성 파일 | 용도 |
|---|---|
| `artifact-manifest.json` | 파일별 해시·artifact ID·구성/구현 hash·Workflow Type·Queue |
| `configuration-private.json` | 네 문서를 컴파일한 실행 정의·private binding·설치 증거 |
| `chain/` | v0.4 구현·Schema·공통 계약의 고정 사본 |
| 승인 descriptor의 파일 | 선택 모듈·필요한 고정 fixture. 전체 demo/tests를 복사하지 않음 |
| `environment.json`, `requirements-frozen.txt` | 생성 환경의 Python patch·기본 runtime 패키지 exact 버전 |
| `artifact_loader.py` | 파일 무결성·환경·구성·구현 pin 검사 후 구성 복원 |

초기 환자 입력·History·receipt DB·가상환경·`.env`·서비스 객체는 자동 포함하지 않는다.
출력 디렉터리·하위 디렉터리는 0700, 파일은 0600으로 생성한다.
승인된 파일이 비밀값을 포함하는지 자동 판별하는 도구는 아니다. 승인 모듈과 작성 설정에
비밀값을 적지 않고 API 자격정보·client·저장소를 Worker의 `services`로 공급한다.
Python 모듈 자체도 신뢰할 배포 입력이다. 승인 목록은 Python 보안 격리가 아니다.

원래 YAML은 컴파일된 설정과 별도로 변경 이력에 보존한다. Artifact를 편집하지 않는다.
새 규칙·모듈·환경은 새 검증·구성·Artifact를 생성한다. 해시는 내용 동일성의 식별자이며 서명이 아니다.
승인한 `artifact_id`를 배포 설정 등 별도 신뢰 경로에 보관하고 로더에 대조한다.

## 독립 프로세스에서 복원

생성 환경과 같은 Python patch·패키지 버전을 준비한다.
추가 모듈이 사용하는 SDK와 OS 라이브러리·인증정보는 배포 환경에서 별도로 고정·제공한다.
기본 패키지 목록은 전이적 의존성을 전부 발견하거나 OS 이미지까지 보존하지 않는다.

아래는 **백엔드/Worker 진입점에 넣을 연결 예시**다. 먼저 실행할 프로세스에서 프로젝트를 import하지 않는다.
`FACT_ARTIFACT`와 `APPROVED_ARTIFACT_ID`는 승인 배포 설정에서 받는다.
`application_services`는 각 모듈 factory가 요구하는 API client·Store·receipt 경로 등을 채운 dict다.

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(FACT_ARTIFACT).resolve()))
from artifact_loader import load
configuration, installation_root = load(expected_artifact_id=APPROVED_ARTIFACT_ID)
from chain.worker import create_worker

# async 함수 안: client는 올바른 주소·namespace로 연결한 Temporal Client
worker = create_worker(client, configuration,
    installation_root=installation_root, services=application_services)
await worker.run()
```

예시의 대문자 값·`client`·`application_services`는 통합 코드가 제공해야 한다.
단순 import/설정 복원만으로 서비스를 주입하거나 병원 API를 연결하지 않는다.
데모 provider는 `store`가 필요하므로 기본 `chain.worker` CLI에 데모 YAML만 지정하면
서비스 미제공으로 실패한다. 완성된 합성 조립 예시는 `demo.runtime`을 사용한다.
독립 Worker는 `create_worker(..., services=...)`로 조립한다.

로더는 자기 manifest·각 파일·선택한 외부 승인 ID·Python/패키지 버전·구현/구성 pin을 검증한다.
다른 경로의 `chain/demo`를 이미 import한 프로세스에서는 거부한다.
loader 자체를 실행하기 전에도 Artifact 배포물과 승인 ID를 신뢰할 경로로 받아야 한다.
운영 이미지의 쓰기 권한·서명·접근 제어를 대신하지 않는다.

## 신규 접수 전환과 이전 실행 유지

1. 새 구성으로 설치·승인·실행 시험과 필요한 수용 절차를 완료한다. Mock/미연동 범위를 기록한다.
2. 구성 전용 Queue에서 고정 Artifact Worker를 시작한다. 백엔드도 같은 구성으로
   `start_episode(client, configuration, initial, engine_run_id=...)`를 호출하도록 배포한다.
3. 접수 경로 전환은 새 Episode에만 적용한다. 이미 열린 실행은 기존 ID·정의·Queue·Worker 코드로 처리한다.
4. 기존 실행/History용 소스·설정·설치 모듈·환경·초기 입력·History의 보존 위치와 접근 권한을 유지한다.
5. 이전 버전으로 신규 접수를 돌려도 새 버전에서 시작한 Run을 구 엔진으로 재해석하지 않는다.
   진행 Run 처리·종료·이관은 별도 절차로 결정한다. 자동 마이그레이션은 제공하지 않는다.
6. 이전 YAML·Artifact는 활성 실행·Replay·복구 참조가 없음을 확인한 뒤 별도 승인 범위에서 정리한다.

기존 v0.3과 통합 전 사실 기반 실행은 원래 소스·설정·승인 모듈·History·환경으로 유지한다.
현재 패키지 이름이나 Queue를 과거 실행에 대입하지 않는다. 보존 Artifact는 새 실행용 묶음에 포함하지 않는다.

## 현재 전환 상태와 검증 경계

| 범위 | 현재 상태 |
|---|---|
| 명시적 사실 기반 Start/Worker·최소 데모 | 구현. Local과 실제 Service 연결·저장 History Replay를 별도 시험 |
| YAML+새 State/마일스톤/사실/승인 Agent 확장 | 비임상 합성 사례로 시험. 코어·Temporal 파일 변경 없이 적용 |
| 기존 v0.3 실행 | 보존 Artifact/별도 원본 환경으로 복원. 현재 기본 실행은 v0.4이며 기존 Run을 자동 이관하지 않음 |
| 사람의 직접 데모 수용 | 미수행. 자동 recorded/stdin은 사람의 확인을 대신하지 않음 |
| 운영 신규 접수 전환 | 미수행. 최소 합성 범위의 자동 안정화 검증 뒤에도 사람의 수용·실제 연동·운영 배포 승인은 별도로 수행 |
| 병원/Agent API·사용자 인증·HTTP/Push·운영 receipt | 미연동. Signal·Query와 모듈 계약을 바탕으로 통합 필요 |
| 확장 임상 승인 | 입원/전원 전체·기록 갈음·유효기간·휴면/재개·사후 정정 등 승인·구현 경계를 별도 검토 |

U1 직전 재확인·독립 보정, U2 실행 완료 후 최신 재실행, U3 근거 개정은 새 경로의 확정 동작이다.
일반 임상 명세 전체를 구현했다고 확대 해석하지 않는다. 현재 Episode 수명은 ACTIVE/COMPLETED뿐이며
완료 Workflow에 후속 Event를 적용하거나 자동 재개하지 않는다.

Worker 객체를 정상 종료한 후 재생성한 시험과 OS 강제 종료/Service DB 장애는 다르다.
현재 Workflow는 입력 두 건을 처리한 뒤 `chain.fact.yield_turn_v1` Activity의 짧은 응답을 기다려
실행 기반에 제어를 반환한다. 이 Activity는 원천 조회·Agent·알림·표시를 수행하지 않는다.
`create_worker()`와 `FactActivities.registered`가 함께 등록하며, 직접 Worker를 구성할 때도 등록을 유지한다.
현재 기술 제한은 시도당10초·전체30초·최대2회다. 임상 Timer나 자동 승인 조건이 아니며 YAML 임상 작업으로 선택하지 않는다.
처리 한 건 자체의 과도한 비용이나 큰 Query의 비용까지 없애는 보장은 아니다.
기존 History의 실행 경로는 startup patch marker로 고정하고 이전 Artifact도 보존한다.
재시도는 같은 논리 요청 ID를 사용하지만 외부 API의 exactly-once를 보장하지 않는다.
실제 Notifier는 현재 CONSOLE/LOG+MOCK_RECORDED 계약만 지원한다. 실제 전달·수신확인 상태에는 계약 확장이 필요하다.

## 데이터와 장기 운영

### 원천자료 조회 주기

기본 주기는 Deployment YAML의 `reconciliation.poll_interval_s: 30`이다.
현재 기본 프로필은 `config/minimum/deployment.yaml`, 음성 프로필은 `deployment-negative.yaml`을 사용한다.
조회 주기가 끝난 시점부터 다음 조회까지의 대기시간이며, 같은 Episode·출처의 주기적 조회를 겹쳐 시작하지 않는다.
값을 수정한 뒤 새 구성으로 Worker와 Episode를 시작한다. 이미 열린 Run의 고정 설정은 바뀌지 않는다.
HITL 보류·재알림 같은 임상 시간은 Clinical의 `timers`에 별도로 둔다.

주기적 조회와 작업 직전 재확인은 같은 백엔드 client의 `fetch_source`를 사용한다.
작업 직전 조회를 최근 polling 결과로 임의 대체하지 않는다. 순차 작업은 재조회하고 실제 병렬 준비만 공유한다.
같은 출처의 polling 중복 방지가 다른 환자·Worker·직전 재확인의 전체 동시 요청 수를 제한하지는 않는다.
백엔드 client에는 개별 요청 timeout을 두고, 운영 연결에서 전체 호출량 제한·시작 시점 분산·서버 제한에 따른
재시도 간격을 제공한다. 이 기능과 실제 FHIR 성능 검증은 현재 합성 client에 포함되지 않는다.
30초 설정은 EMR 변경 후 최대 30초 안의 반영을 보장하지 않는다. 병원→FHIR 동기화와 조회·처리 지연도 포함된다.

변경 조회 자체가 실패하면 다음 주기에 다시 조회한다. 발견 Event의 정확한 버전 조회가
기존 최대 시도/기한을 모두 소진해 FAILED가 되면, 같은 Event 재발견으로는 자동 복구하지 않는다.
`event_receipts`와 `source_reconciliation`을 감시하고 별도 운영 재처리 계약을 마련해야 한다.
실패를 숨기려고 새 Event ID를 발급하거나 같은 참조에서 최신 버전으로 바꿔 읽지 않는다.

### 저장과 부하

Temporal History는 실행 재생을 위한 기록이며 병원 원천 저장소와 목적이 다르다.
현재 facts/원문/선행 결과가 Workflow 상태·Activity payload·History에 들어간다.
환자 자료가 Temporal에 전혀 저장되지 않는다고 설명하면 안 된다.
외부 원천 버전 저장·권한·보존 정책과 Snapshot 외부 참조는 운영 통합에서 구체화한다.

데모 SQLite receipt는 별도 private 경로에 두고 Artifact에 넣지 않는다.
실제 전달 시스템을 붙여도 재시도·중복 효과·전달 상태를 관리할 receipt의 책임은 남는다.
별도 업무 PostgreSQL이나 메시지 브로커는 현재 엔진의 필수 구성요소가 아니다.
Temporal 영속 Service의 DB는 배포 환경에서 따로 운영한다.

전체 Snapshot·상세 원문을 화면에 매번 보내는 방식은 장기 운영용 DTO가 아니다.
반복 조회는 `progress`, 정확한 근거 열람은 `hitl_view`를 사용한다. 전체 진단·종료 결과·History는 유지한다.
데모의 진행 조회는 일시 오류에 최대3회 재시도하며 각 조회의5초 제한과 임상 자료 재확인 정책은 유지한다.
실패·회복·재시도 소진은 `query-diagnostics.json`, 종료 자료 수집은 `close-diagnostics.json`에 기록한다.
Snapshot 조회가 실패해도 History·실행 상태·Replay 수집은 별도로 시도하며, 원래 실패를 종료 오류로 덮지 않는다.
최소 시험에서도 큰 payload 경고를 관찰했다. History 정리/Continue-As-New·대용량/장기 부하·OS 장애 복구는
검증되지 않았다. 장기·민감 데이터가 있는 실제 운영 전에는 이 한계를 해소하고 별도로 검증한다.
