# 사실 기반 엔진 연동 안내

이 문서는 `ChainFactEpisodeV1`을 백엔드·원천 시스템·Agent에 연결할 때 사용하는 현재 Python 계약을 정리한다.
YAML 작성은 [작성 안내](fact_authoring.md), 합성 실행은 [데모 안내](../../demo/README.md)를 따른다.
아래 함수와 필드는 현재 코드에 있는 계약이다. 병원 API, 사용자 인증, HTTP·WebSocket 서버가 구현됐다는 뜻은 아니다.

## 1. 연결할 작업과 코드 위치

| 할 일 | 확인할 코드 | 구현할 부분 |
|---|---|---|
| Episode를 시작한다 | `chain/client.py:start_episode`, `chain/execution.py:build_start` | 백엔드의 생성 요청·식별자 관리 |
| 병원 자료의 변경을 조회한다 | `chain/backend_source.py:BackendSourceProvider`, `chain/reconciliation.py` | 백엔드 client의 `fetch_source`·checkpoint 연결 |
| 작업 직전 최신 자료를 확인한다 | `chain/worker_runtime.py:preflight`, `chain/backend_source.py` | 주기적 조회와 같은 client의 `fetch_source` 호출 |
| 수동 Event를 넣는다 | `chain/temporal_workflow.py:submit_event`, `chain/worker_runtime.py:resolve_event` | 이미 공개된 정확한 버전의 표준 Event 전송 |
| Agent 구현을 연결한다 | `chain/task_runtime.py:NativeAgentProvider`, `chain/worker_runtime.py` | 동기 factory·invoke·개별 입출력 검증 |
| 알림·EHR 표시를 연결한다 | `chain/task_runtime.py:invoke_work`, `demo/mocks/notifier.py`, `demo/mocks/effects.py` | 현재 Mock의 실제 전달/표시 경계와 새 상태 계약 |
| HITL 화면과 응답을 연결한다 | `chain/temporal_workflow.py`, `chain/hitl.py:HITLOwner` | 최신 근거 표시·인증된 사용자·응답/접수 결과 처리 |

모든 외부 I/O와 모듈 import는 Worker/Activity 측에 둔다.
`EpisodeEngine`과 Workflow에 병원별 API 호출이나 개별 Agent 분기를 추가하지 않는다.

## 2. Episode를 생성하고 Worker를 연결한다

### 생성 입력

백엔드는 JSON 파일 대신 아래 8개 키를 가진 Python dict/JSON을 전달한다.
`initial.json`은 데모가 같은 입력을 읽는 파일일 뿐, 운영 백엔드에 요구하는 파일 인터페이스가 아니다.

```json
{
  "site_id": "HYUMC_GURI",
  "patient_id": "PT-HYG-edd5a27af1",
  "encounter_id": "E261006058",
  "episode_id": "EP-HYG-261006-058",
  "age": 68,
  "sex": "F",
  "bed": "소생실-02",
  "arrival_time": "2026-10-06T15:12:41+09:00"
}
```

이 값은 현재 합성 Episode의 예다. 식별자·성별·병상은 빈 문자열이 아닌 문자열,
나이는 0 이상의 정수, 도착시각은 시간대가 있는 ISO 8601 문자열이다.
검증 함수는 `chain.runtime_contracts.validate_initial`이다. 추가 키도 거부한다.
초진기록·검사결과·미래 일정·Agent 정답을 초기 입력에 섞지 않는다.

### 실행 연결

백엔드와 Worker는 이미 실행 중인 Temporal Service에 Python SDK로 연결한다.
`CHAIN_TEMPORAL_SERVER_PATH`는 데모의 `--start-local`이 임시 Service를 시작할 때만 사용하는 CLI 실행 파일 경로다.
아래 `start_episode`·`create_worker`·`integration.worker` 경로는 CLI 파일을 찾거나 실행하지 않는다.
Service의 주소·namespace·필요한 인증을 배포 설정에서 제공해야 한다.

```python
from temporalio.client import Client

# 백엔드/Worker의 async 초기화 코드. 실제 Service 주소와 namespace를 사용한다.
client = await Client.connect("localhost:7233", namespace="default")
```

백엔드와 Worker는 같은 Service·namespace와 같은 configuration의 Queue를 사용한다.
Task Queue는 구성 hash로 계산하므로 임의의 이름으로 바꾸지 않는다.
`localhost`는 각 서버/컨테이너 자신의 주소이며 다른 환경의 Service를 자동으로 가리키지 않는다.
SDK의 TLS·API key 설정이 필요한 환경은 Client 초기화에 연결한다. 현재 Worker CLI의
인자는 `--address`·`--namespace`이며 TLS·API key 플래그는 없으므로 해당 bootstrap 확장이 필요하다.
현재 데모 `--address` 연결은 기본 `default` namespace만 사용하고 이 인증 옵션을 제공하지 않는다.

백엔드와 Worker는 같은 검증된 configuration을 사용한다. 아래 코드는 연결 형태를 보여주는 발췌다.
`configuration`은 `chain.configuration.load_configuration`으로 검증한 패키지,
`client`는 이미 연결한 `temporalio.client.Client`다. Artifact 고정·배포 절차는 [운영 안내](fact_operations.md)를 따른다.

```python
from chain.client import start_episode

handle = await start_episode(
    client, configuration, initial,
    engine_run_id="chain-run-unique-id",
)
```

Workflow ID는 `engine_run_id`다. 내부 Run Manifest에는 4개 식별자와 설정·구현 pin을 고정한다.
Task Queue는 `queue_for_configuration(configuration)`이 설정 hash로 계산한다.
동일 Workflow ID의 중복 시작은 거부하지만, 같은 `episode_id`에 다른 실행 ID를 발급하는 중복 생성까지 막아주지는 않는다.
백엔드가 Episode와 실행 ID의 대응 관계를 관리해야 한다.

Worker 프로세스에서는 다음 진입점을 사용한다.

```python
from chain.worker import create_worker

worker = create_worker(
    client, configuration,
    installation_root=project_root,
    services=worker_services,
)
await worker.run()
```

`worker_services`는 모듈 factory에 주입하는 private dict다. API 클라이언트·인증 설정·저장소 등을
해당 모듈이 합의한 키로 주입한다. 이를 Workflow 입력이나 Signal에 넣지 않는다.
factory와 provider는 동기 함수이며 여러 Activity에서 함께 호출될 수 있으므로 I/O와 상태 공유의 동시성을 관리해야 한다.
Worker의 등록 목록에는 원천/Agent/표시 Activity 외에 내부 실행권 반환용 `chain.fact.yield_turn_v1`도 포함된다.
이 응답은 임상 작업 결과나 Agent 성공을 뜻하지 않으며 외부 모듈을 호출하지 않는다.
공통 `create_worker()`를 쓰면 등록된다. 직접 조립할 때에는 `FactActivities.registered`를 유지한다.

현재 합성 factory의 실제 키는 `demo/providers.py`·`demo/run.py`에 있다.
`store`, `notifier_path`, `effects_path`가 필요하고 `write`, `now`, `source_calls`는 사용 지점에 따라 선택적이다.
합성 설정은 `store` 등이 필요하므로 완성된 데모 조립은 `python -m demo`를 사용한다.
통합 Worker의 서비스 주입 진입점은 `integration/worker.py`다.
운영자가 신뢰하는 `module:function`을 `--services-factory`로 지정하면
`factory(configuration=..., installation_root=...)`가 반환한 private dict를 Worker에 공급한다.
이 함수는 배포 프로세스의 초기화 코드이며 병원 Event나 임상 YAML에서 선택하지 않는다.
반환값은 기존과 동일하게 API client·저장소·인증 설정 등을 담는 dict다. 개별 모듈 구현과 키를 맞춘다.
`python -m chain.worker`의 기본 CLI에는 이 주입 인자가 없으므로, 서비스가 필요한 구성은
`integration.worker` 또는 위 `create_worker(..., services=...)`로 시작한다.

```sh
.venv/bin/python -m integration.worker \
  --clinical config/minimum/clinical.yaml --site config/minimum/site.yaml \
  --safety config/minimum/safety.yaml --deployment config/minimum/deployment.yaml \
  --metadata config/minimum/module_metadata.json --installed config/minimum/installed.json \
  --installation-root . --services-factory application_services:build_services \
  --address localhost:7233 --namespace default
```

`application_services`는 통합 애플리케이션이 구현할 모듈명 예시다.
현재 minimum 설정은 합성 Provider이므로 이 명령만으로 병원 API에 연결되지는 않는다.
운영 구현을 선택할 때 Deployment·메타데이터·승인 목록과 서비스 구성을 함께 교체한다.

## 3. Event·결정·화면 조회를 백엔드에 연결한다

현재 사용하는 Temporal 입력/조회는 다음과 같다. Workflow Update나 HTTP endpoint는 구현돼 있지 않다.

| 호출 | 전달값 / 결과 |
|---|---|
| `handle.signal("submit_event", {"source_id": ..., "event": ...})` | Site의 논리 Source ID와 변경하지 않은 표준 Event |
| `handle.signal("submit_decision", decision)` | `chain-fact-hitl-decision/v1` |
| `handle.signal("request_hitl_resume", command)` | `chain-fact-hitl-resume/v1`. 보류 후 조기 재질문 요청 |
| `handle.signal("source_reconnected", source_id)` | 선언된 논리 Source ID. 보정 주기를 요청 |
| `handle.query("snapshot")` | 현재 State·사실·작업·HITL·Timer·접수/처리 결과 |
| `handle.query("changes_since", view_seq)` | 해당 증가 순번 이후 화면 변경 기록 목록 |
| `handle.query("progress")` | `chain-fact-progress/v1`. 진행·상태·근거 개정 식별자. 원문·Agent 결과 본문·전체 실행 입력은 제외 |
| `handle.query("hitl_view", args=[request_id, revision])` | `chain-fact-hitl-view/v1`. 지정 질문/개정의 정확한 근거·원문·관련 결과/직전 확인 |

Signal 전송 성공은 임상 결정 수락이나 자료 반영 완료가 아니다.
`progress.ingress_receipts` 또는 `snapshot.ingress_receipts`에서 제출 식별자에 대응하는 엔진 접수 결과를 확인한다.
Event는 `event_receipts[event_id]`의 적용 결과도 확인한다.
완료된 Workflow는 후속 Event를 계속 처리하는 endpoint가 아니다.

프론트엔드에는 백엔드가 필요한 화면 데이터만 골라 전달한다.
반복 화면 갱신은 `progress`, 질문·근거 개정의 표시와 열람은 `hitl_view`를 사용한다.
기존 `snapshot`은 전체 진단/결과 저장용으로 유지한다. 다음 표는 전체 조회의 필드 위치이며,
경량 조회에서 빠지는 본문을 임의로 채우지 않는다.

`progress`는 일부 object/array 값을 생략하고 긴 문자열을 줄여 표시한다. `value_omitted`·
`value_truncated`로 구분하며 완전한 Fact Snapshot·Agent Result로 해석하지 않는다.
Agent 입력은 기존 작업 준비/원천 재확인 계약으로 만든다.

새 질문/개정의 폼을 만들 때는 정확한 `revision`을 지정해 상세 조회하고 기존 Snapshot·원문 검증을 적용한다.
그 사이 현재 개정/상태가 바뀌면 폼을 다시 바인딩한다. 과거 개정 열람은 가능하나 결정 제출은 최신 개정에만 한다.
이 조회 분리가 실행 직전 원천 재확인이나 HITL 응답 검증을 생략하지 않는다.

| 화면에 필요한 정보 | 현재 Query 위치 |
|---|---|
| 현재 표시 위치·계산 근거 | `state`, `projection` |
| 최신 사실과 미확보/충돌 여부 | `facts.facts.<Fact ID>.value/validity/source_refs` |
| Agent 진행·결과·현재/참고 구분 | `tasks`, `executions`의 `result`·`result_use` |
| 열린 질문·현재 근거 개정 | `hitl.requests`, `hitl.evidence` |
| 근거 원문 | 해당 evidence의 `source_documents`. Snapshot의 구조화 값과 별개 |
| 대기시간 | `timers.<ID>.payload.due_at/owner`와 기록의 `status`. 만료가 자동 승인이나 State 전진을 뜻하지 않음 |
| 입력 접수와 Activity 상태 | `ingress_receipts`, `event_receipts`, `activity_progress` |
| 최신 여부 확인·누락 보정 | `verifications`, `source_reconciliation` |

`state`는 표시 이름이 아니라 실행에 고정한 Clinical의 `states` key다.
현재 최소 프로필의 `ST-…` ID와 임시 NO 분기 `S3N`은 [State 식별자](../../demo/README.md#state-식별자)를 따른다.
화면의 State 매핑은 이 ID를 사용하고, 이전 구성으로 시작한 실행의 ID를 최신 값으로 바꿔 해석하지 않는다.

Query 자체에 사용자별 조회 권한 필터나 WebSocket/SSE 전송은 없다. 인증·인가·화면별 필드 선택·Push는 백엔드에서 연결한다.

## 4. 원천 시스템 Adapter를 구현한다

원천자료의 운영 조회 경로는 `CHAIN → 백엔드 API → FHIR 서버`다.
병원 자료를 FHIR 서버에 반영하는 경로와 지연은 병원 연동에서 제공한다.
병원에서 변경 알림을 보내지 않아도 CHAIN의 주기적 조회로 자료를 발견한다.
Agent·HITL·알림·표시 작업을 준비할 때에도 같은 조회 경로로 최신 여부를 확인한다.
두 시점에 별도의 원천 연동 코드를 만들거나, 백엔드가 Context를 직접 덮어쓰는 구조가 아니다.

`chain/backend_source.py:BackendSourceProvider`가 아래 세 메서드를 제공한다.
주기적 조회와 작업 직전 조회는 모두 주입한 백엔드 client의 `fetch_source(request)`를 호출하며,
Provider가 반환값을 기존 엔진 계약으로 변환한다. 호출부와 최종 검사는 `FactWorkerRuntime`에 있다.
현재 합성 연결은 `demo/providers.py:PublishedSource`와 `demo/backend.py`를 참고한다.

| 메서드 | 입력 | 반환 |
|---|---|---|
| `resolve_exact(event)` | 수신한 표준 Event | 해당 기록/버전의 `chain-context-resolved/v0.3` completion |
| `query_latest(request)` | `chain-fact-preflight-request/v1` | `{result: <preflight result>, events: [{source_id, event}, ...]}` |
| `reconcile_changes(request)` | `chain-fact-reconcile-request/v1` | `{result: <reconcile result>, events: [{source_id, event}, ...]}` |

### 백엔드 client를 연결한다

통합 애플리케이션은 동기 `fetch_source(request)`와 `resolve_exact(event)`를 가진 client를 구현한다.
이를 Worker 초기화에서 `services["source_backend"]`로 제공하고, 승인된 Source 모듈의 entrypoint로
`chain.backend_source:provider_factory`를 선택할 수 있다.
현재 minimum 설정은 합성 Provider를 선택한다. 실제 client로 교체할 때는 Deployment의
`source_bindings`와 설치 descriptor의 entrypoint·구현 파일·출처/자료 범위 승인을 함께 맞춘다.
API URL·인증정보·client 객체는 임상 YAML이나 Workflow 입력에 넣지 않는다.

`fetch_source`의 Python/JSON 계약은 `chain-fact-source-fetch-request/v1`이다.
실제 HTTP 주소·인증·FHIR 리소스 변환은 이 client 안에서 연결한다. 아래 계약을 FHIR 서버가 그대로
제공한다고 가정하지 않는다. Provider에는 CHAIN 형식으로 정규화한 결과를 반환한다.

| 요청 필드 | 전달하는 내용 |
|---|---|
| `contract_schema` | `chain-fact-source-fetch-request/v1` |
| `request_id`, `identity`, `engine_run_id`, `run_pin` | 이번 요청·환자/내원/Episode·고정 실행 식별자 |
| `source`, `scope`, `authorization_ref` | 논리 출처·확인할 Fact 목록·허용된 조회 범위의 참조 |
| `purpose` | 주기적 조회는 `RECONCILIATION`, 작업 준비는 `AGENT_INPUT/HITL_EVIDENCE/DELAY_NOTICE/EHR_EFFECT` |
| `basis.known_versions` | 작업 준비 시 이미 확보한 기록 버전. 단순 SourceRef 목록이며 hash 목록이 아님 |
| `basis.checkpoint` | 변경 조회·페이지 진행의 출처 소유 cursor. 없으면 null |
| `limits` | 이번 호출의 시간·시도·페이지·준비 round 제한 |

응답은 `chain-fact-source-fetch-result/v1`이다. 요청 식별 필드와 `purpose/source`를 그대로 돌려주고,
다음 필드를 제공한다. 필수·추가 키, 환자·출처·버전·hash는 Provider와 Worker에서 검증한다.

| 응답 필드 | 반환하는 내용 |
|---|---|
| `status` | `COMPLETE/PARTIAL/FAILED`. 응답이 비었다는 이유로 COMPLETE라고 추정하지 않음 |
| `coverage` | 요청 항목마다 `{field, verification: VERIFIED|UNCONFIRMED}`. VERIFIED는 조회 확인이며 자료 존재·임상 정상 판정이 아님 |
| `events` | 안정적인 ID·정확한 버전/hash를 가진 표준 `{source_id, event}` 목록 |
| `checkpoint` | 이번 조회의 후보 cursor. 전체 반영 확인 전까지 확정 cursor가 아님 |
| `source_basis` | `{kind: VERSION_TOKEN|CURSOR|OBSERVATION, token, observed_at}` |
| `checked_at` | 이번 확인이 끝난 시각. 기존 Event의 수신 시각을 이 값으로 바꾸지 않음 |
| `errors` | 미확인 범위와 이유 `{scope, code, retryable}` |

백엔드 client 또는 그 안의 변환 모듈이 원천 기록을 표준 Event로 정규화한다.
FHIR의 문자열 버전 ID와 현재 CHAIN의 양의 정수 version 사이의 안정적인 대응도 이 경계에서 제공한다.
같은 기록 버전을 다시 읽어도 Event ID·내용·원천 시각은 유지해야 한다. 조회 시각만 바뀌었다고
새 기록 버전이나 Event를 만들지 않는다. 같은 버전의 내용이 바뀌는 오류를 known_versions 필터로 숨기면 안 된다.
새 정정·철회·충돌은 명시적인 근거와 대상 버전을 포함한 새 Event로 제공한다.

발견 Event는 두 조회 모두 같은 Ingress와 `resolve_exact` 경로로 반영한다.
`resolve_exact(event)`는 Event가 지정한 기록·버전·hash의 completion을 반환해야 한다.
이 사이에 새 자료가 생겨도 최신 버전으로 바꿔 반환하지 않는다. FHIR/백엔드의 버전 조회나
공유된 불변 버전 보관 기능으로 보장하며, 재시작하면 사라지는 Worker 메모리만으로 구현하지 않는다.
반복 조회에서 자료가 보이지 않는 것만으로 삭제·철회를 추정하지 않는다.

통신 불가·timeout 같은 예상된 조회 불가는 client가 `BackendUnavailable(code, retryable=...)`로 알린다.
Provider는 이를 FAILED와 UNCONFIRMED로 변환한다. 확보된 자료는 보존하며 임의의 성공값을 넣지 않는다.
다른 환자·출처, 잘못된 schema·버전·hash는 계약 오류로 거부한다.
한 Activity의 timeout이 이미 실행 중인 blocking HTTP 호출을 강제로 중단해 주지는 않으므로,
client에도 연결/응답 timeout을 둔다. 공통 Provider가 임의로 API 재시도를 추가하지 않는다.
여러 Episode/Worker의 동시 호출 제한·분산·서버 제한에 따른 재시도 간격은 실제 백엔드 연결 시 제공한다.

변경 조회 자체의 실패는 다음 polling에서 새 조회를 시도한다.
발견 Event의 정확한 버전 조회는 기존 유한 재시도만 사용한다. 그 한도나 기한을 소진해
Event가 FAILED가 된 경우, 같은 Event를 다시 발견하는 것만으로 재시도 예산을 초기화하지 않는다.
현재 자동 복구/재처리 명령은 없으므로 `event_receipts`와 `source_reconciliation`의 실패 상태를 확인해야 한다.
Event ID를 바꿔 같은 실패를 새 자료로 가장하거나 최신 버전으로 대체하지 않는다.
운영용 재처리 절차는 별도 계약과 시험이 필요하다.

조회 주기는 사용하는 Deployment YAML의 `reconciliation.poll_interval_s`이며 기본은 30초다.
조회 주기 종료 후의 대기시간이며 이미 열린 실행에는 YAML 편집을 자동 적용하지 않는다.
값의 변경 방법은 [README](../../README.md#원천자료-조회와-주기-설정)를 따른다.
주기적 조회를 최근에 했더라도 순차 작업 직전에는 다시 조회한다. 실제 병렬 준비만 결과를 공유한다.

### 변경 알림은 정확한 버전을 조회할 수 있게 공개한 뒤 보낸다

Event는 `chain-event/v0.3`을 그대로 재사용한다. Agent Request의 v1과 혼동하지 않는다.

| 필수 필드 | 의미 |
|---|---|
| `contract_schema`, `event_id`, `event_type` | 형식 `chain-event/v0.3`, 재전송 시 유지할 Event ID, 사건 종류 |
| `site_id/patient_id/encounter_id/episode_id` | 실행 중 Episode와 같은 4개 식별자 |
| `source_ref` | `{system, record_id, version}`. version은 양의 정수 |
| `sequence_no` | 양의 정수. 별도 `source_sequence_no`는 선택적 원천 순번 |
| `source_event_time` | 원천에서 발생한 시각 |
| `published_at/emitted_time/received_at` | 조회 공개·전송·수신 시각. 시간대가 있는 ISO 8601 |
| `payload` | 정확히 `{data_ref: <source_ref와 동일>, content_hash: <원천 내용 hash>}` |
| `payload_hash` | `canonical_hash(payload)` |

SourceRef는 환자 식별자를 대체하지 않는다. `source_id`는 Site의 논리 출처,
`source_ref.system`은 그 출처가 사용하는 실제 시스템 이름이다.
원천이 최신 버전만 제공하면 과거 Snapshot의 버전을 재현할 수 없으므로 정확한 버전 조회/보존 계약이 필요하다.

completion은 다음 필드를 가진다.
`completion_schema`, 4개 식별자, `event_id`, `event_type`, `sequence_no`, `source_ref`,
`source_time`, `published_at`, `received_at`, `content_hash`, `facts`, `payload`, `completion_hash`.
`completion_schema`는 `chain-context-resolved/v0.3`, `completion_hash`는 자기 필드를 제외한 내용 hash다.
`facts.<이름>`은 `value/status/source_ref/source_time/known_at`을 필수로 가지며
`unit/confirmation_status/dependencies`가 선택적이다. 검증은 `chain.common.source_contracts.validate_completion`을 사용한다.

정정·오류·철회·충돌에는 completion의 선택적 `revision: {kind, targets, reason}`을 사용한다.
kind는 `CORRECTION/ERROR/RETRACTION/CONFLICT`이며 이전 동일 기록 버전의 구체적인 필드 SourceRef를 targets에 넣는다.
버전 숫자 증가만으로 정정이나 철회라고 추정하지 않는다.

### 실행 준비와 누락 보정의 반환값을 구분한다

preflight request의 공통 필드는 `contract_schema/request_id/identity/engine_run_id/run_pin`이다.
추가 필드는 다음과 같다.

| 필드 | 용도 |
|---|---|
| `preparation_id/parallel_batch_id/consumer_task_ids` | 이번 준비와 실제 병렬 소비 작업 묶음 |
| `purpose` | `AGENT_INPUT/HITL_EVIDENCE/DELAY_NOTICE/EHR_EFFECT` |
| `source/scope/authorization_ref` | 논리 출처·조회 Fact 목록·고정된 조회 승인 참조 |
| `basis` | `data_epoch/known_versions/source_cursor`. 엔진이 이미 가진 자료 기준 |
| `limits` | `timeout_ms/max_attempts/max_pages/max_preparation_rounds` |

preflight result는 공통 필드와 `status/coverage/event_refs/source_basis/checked_at/errors`다.
status는 `NO_CHANGE/CHANGES_FOUND/PARTIAL/FAILED`, coverage는 모든 요청 필드의
`{field, verification: VERIFIED|UNCONFIRMED}`다. 미확인 필드는 `errors`의 `{scope, code, retryable}`로 설명한다.
`source_basis`는 `{kind: VERSION_TOKEN|CURSOR|OBSERVATION, token, observed_at}`이며 관찰만 했다면 token은 null이다.

reconcile request는 공통 필드와 `source/scope/authorization_ref/checkpoint/limits`다.
result는 공통 필드와 `source/status/from_checkpoint/candidate_checkpoint/event_refs/errors`를 반환한다.
status는 `COMPLETE/PARTIAL/FAILED`다. 후보 checkpoint는 발견된 Event의 실제 반영을 확인한 뒤 확정한다.

두 조회 모두 `event_refs`는 `{event_id, source_ref, content_hash}` 목록이고,
동반 `events`와 정확히 일치해야 한다. 발견 자료를 결과 dict에서 Context에 직접 덮어쓰지 않는다.
같은 Ingress→정확한 버전 resolve 경로로 반영한다. 조회 실패는 확보 자료+최신 여부 미확인으로 기록한다.
실제 병원 API의 페이지·변경 cursor·삭제/정정 모델은 해당 Source 구현에서 연결해야 한다.

preflight는 Agent·HITL·알림·표시 작업 직전에 엔진이 발행한다.
확인 결과는 호환되는 실제 병렬 준비 묶음에서만 공유하고 순차 작업은 다시 조회한다.
Summary 캐시가 있더라도 필요한 원천 재확인을 생략하는 계약은 아니다.

## 5. Agent의 invoke와 개별 검증기를 연결한다

### 모듈 factory와 실행 함수를 나눈다

설치 entrypoint는 동기 `provider_factory(binding, services)`다.
`binding`에는 선택된 private binding과 `kind/logical_id/implementation_hash`가 들어온다.
Agent인 경우 `NativeAgentProvider` 또는 동일한 명시적 계약을 제공하는 객체를 반환한다.

```python
from chain.task_runtime import NativeAgentProvider

provider = NativeAgentProvider(
    agent_id=agent_id, version=version,
    implementation_hash=binding["implementation_hash"], modes=modes,
    invoke=invoke,
    input_validator=validate_input,
    output_validator=validate_output,
)
```

`invoke(request, snapshot, services)`의 반환값은 Agent 고유 payload dict다.
공통 Result envelope는 `LocalTaskRuntime.invoke_agent`가 결합한다.
`validate_input(request, snapshot)`과 `validate_output(payload, request, snapshot)`은 필수 callback이다.
metadata의 `output_fields`는 이름 집합만 검사하므로 각 값의 타입·enum·내부 구조는 이 callback에서 검증해야 한다.

### request·snapshot·services의 차이

| 인자 | 실제 내용 |
|---|---|
| `request` | 실행 식별·Agent/mode·버전·입력 fingerprint·근거·선행 결과 ID·확인 ID와 Snapshot을 가진 JSON dict |
| `snapshot` | `request.snapshot`의 분리된 복사본. 이 작업 범위의 원천 Fact와 버전/상태 |
| `services` | Worker 안에서만 쓰는 `AgentServices` 객체. JSON/Workflow 입력이 아님 |

invoke의 services와 factory의 services dict는 서로 다른 객체다.
현재 invoke services는 `cache`, `backend`(현재 null), `source_snapshot`, `prerequisite_results`,
`verifications`, `structured_contexts`를 제공한다. 선행 결과/확인 기록은 요청의 ID 집합과 일치하는 승인된 복사본이다.
`structured_contexts`는 선행 Summary 결과에서 `structured_context`를 뽑은 것이며 key는 결과 실행 ID다.
API 클라이언트가 invoke services의 backend에 자동 연결되지는 않는다. factory의 private services에서 받아
개별 provider의 멤버/closure에 연결한다. Workflow에 객체를 직렬화하지 않는다.

Agent Request의 전체 필드는 아래와 같다. 엔진이 생성하므로 백엔드가 임의로 채워 실행을 우회하지 않는다.

| 구분 | 필드 |
|---|---|
| 형식·실행 | `contract_schema: chain-fact-agent-request/v1`, `identity`, `engine_run_id`, `run_pin` |
| 작업·호출 | `task_id`, `execution_id`, `request_id`, `input_revision`, `predecessor_execution_id` |
| 구현 | `agent_id`, `agent_version`, `implementation_hash`, `mode` |
| 입력·근거 | `snapshot`, `scope`, `dependencies`, `input_fingerprint` |
| 선행·확인·시각 | `prerequisite_result_ids`, `verification_ids`, `evaluated_at` |

`request_id == execution_id`다. scope는 정렬된 Fact ID 목록이며 Snapshot의 facts와 정확히 일치한다.
dependencies는 Snapshot Fact들의 구체적인 필드 SourceRef 합집합이다.
Snapshot 전체는 `contract_schema/identity/engine_run_id/run_pin/snapshot_id/known_at/facts`를 가진다.
`snapshot_id`와 `input_fingerprint`는 코드가 계산한 내용을 검증한다. ID를 보기 좋게 바꾸려고 해시만 교체하지 않는다.

아래는 `TaskEngine._request`와 같은 키로 Request를 구성하는 형식 예다.
각 변수는 엔진이 발급/고정한 값이다. Agent에서 이 객체를 새로 만들거나 실행 승인을 대신하는 코드가 아니다.

```python
from chain.runtime_contracts import canonical_source_refs
from chain.task_contracts import input_fingerprint

request = {
    "contract_schema": "chain-fact-agent-request/v1",
    "identity": identity, "engine_run_id": engine_run_id, "run_pin": run_pin,
    "task_id": task_id, "execution_id": execution_id, "request_id": execution_id,
    "input_revision": input_revision,
    "predecessor_execution_id": predecessor_execution_id,
    "agent_id": agent_id, "agent_version": agent_version,
    "implementation_hash": implementation_hash, "mode": mode,
    "snapshot": snapshot, "scope": sorted(snapshot["facts"]),
    "dependencies": canonical_source_refs([
        ref for fact in snapshot["facts"].values() for ref in fact["source_refs"]
    ]),
    "prerequisite_result_ids": sorted(prerequisite_result_ids),
    "verification_ids": sorted(verification_ids), "evaluated_at": evaluated_at,
}
request["input_fingerprint"] = input_fingerprint(request)
```

다음은 실제 합성 원천 `nursing_v1.json`의 glucose를 사용하는 **Fact 형식 예**다.
`known_at`은 예시 반영시각이며 실행 로그 발췌가 아니다. 전체 Request/Snapshot을 대신하는 JSON이 아니다.

```json
{
  "glucose": {
    "value": 158,
    "validity": "KNOWN",
    "source_status": "AVAILABLE",
    "source_refs": [{"system": "NURSING_EMR", "record_id": "DOC-2610060403", "version": 1, "field": "glucose"}],
    "source_time": "2026-10-06T15:15:30+09:00",
    "known_at": "2026-10-06T15:15:31+09:00",
    "confirmation_status": "UNCONFIRMED"
  }
}
```

없는 unit을 추가하거나 UNCONFIRMED를 임상 확인 완료로 바꾸지 않는다.
미확보 Fact는 `value: null`과 실제 validity를 사용한다.
공통 검증은 `task_contracts.validate_agent_request/validate_agent_result`, `runtime_contracts.validate_snapshot`에 있다.

### Summary와 결과를 원천에 합치지 않는다

현재 `StructuredSummaryProvider`는 요청한 Fact 범위만 조합한다. NLP 구현이 아니다.
원천 Snapshot은 별도로 보존하고 Summary 결과를 `services.structured_contexts`로 전달한다.
캐시는 의미 입력·범위·Agent/구현 버전·mode에 결합되며 다른 요청의 옛 Snapshot envelope를 반환하지 않는다.

공통 Result 형식은 `chain-fact-agent-result/v1`이며 요청의 실행/구현 식별을 유지하고
`input_snapshot_id/status/result/evidence/produced_at`을 포함한다.
SUCCESS이면 선언된 필드의 payload dict, FAILED이면 `result: null`과 명시적 `error_code`를 사용한다.
fixture 불일치·검증 실패를 음성/PASS로 대체하지 않는다.
실행 중 입력이 바뀌면 기존 호출을 마친 뒤 최신 입력으로 재평가하고 구결과는 참고로 구분한다.
외부 Agent가 자체 비동기 job을 운영하는 callback/cancel 계약은 현재 연결돼 있지 않다.

## 6. HITL 화면과 응답을 연결한다

`hitl.requests[request_id]`의 `current_revision`과 `revision_ids`로 최신 evidence를 선택한다.
근거 갱신 시 같은 열린 질문에 새 개정이 생기며 과거 Snapshot·원문을 수정하지 않는다.
진행 중 준비/실패/OPEN/수락/종료 상태를 구분한다. 구개정의 응답을 자동으로 새 개정에 재제출하지 않는다.

결정 wire의 필수 항목은 다음과 같다.

| 구분 | 필드 |
|---|---|
| 형식·실행 | `contract_schema: chain-fact-hitl-decision/v1`, `identity`, `engine_run_id`, `run_pin` |
| 응답·질문 | `decision_id`, `request_id`, `decision` |
| 확인한 근거 | `evidence_revision`, `evidence_id`, `evidence_snapshot_id` |
| 응답자·확인 | `actor: {role, staff_id}`, `confirmed_items`, `evidence_viewed`, `comment`, `recorded_at` |

`evidence_viewed`에는 현재 evidence_id가 반드시 들어간다. 원문 bundle ID만으로 대체하지 않는다.
구개정·허용되지 않은 역할/선택·필수 확인 누락·갱신 준비 중 응답은 수락하지 않는다.
`ingress_receipts`의 `ACCEPTED/REJECTED/PREPARING`과 code를 확인해 화면을 갱신한다.
같은 decision_id를 재전송할 때는 원래 내용 그대로 유지한다.

보류 후 수동 재질문은 `chain-fact-hitl-resume/v1`을 Signal로 전달한다.
공통 실행 식별과 `command_id/prior_request_id/actor/requested_at`이 필수다.
이는 YES/NO 응답이 아니며 새 질문을 준비하는 명령이다. 데모의 ready도 이 계약을 사용한다.

Site의 role_bindings는 현재 `X: [X]` 형태만 지원한다. 실제 외부 역할 변환·로그인은 구현돼 있지 않다.
백엔드는 인증된 사용자·병원 권한을 확인하고 Safety가 사용하는 표준 role을 전달한다.
문자열 role 검사와 열람 확인값은 실제 인증이나 화면을 읽었다는 증명을 대신하지 않는다.

## 7. 알림과 EHR 표시를 구분한다

### 연결할 책임

운영 알림은 CHAIN이 발송 조건·내용·수신 대상을 정하고 백엔드 Notifier API가 전달하는 구조로 연결한다.
백엔드는 기관 주소록/당직표를 통해 지정된 대상의 실제 전달 주소를 해석한다.
알림의 임상 조건·종류·대상 역할을 백엔드에서 다시 선택하지 않는다.
예를 들어 CHAIN이 환자의 신경과 당직자를 지정했다면 백엔드는 그 대상의 주소를 찾으며,
SMS gateway라는 발송 수단 자체를 수신 의사로 취급하지 않는다.
특정 직원까지 지정하려면 대상 조회 결과와 실제 발송 요청의 연결을 별도로 정의해야 한다.

현재 구현은 template와 논리적 수신처를 선택하는 단계까지다.
본문 생성·구체 대상 조회·실제 발송 API와 비동기 전달 상태 반영은 미구현이다.
[README의 Notifier 연결](../../README.md#notifier-연결)에 현행 형식과 **현재 미지원인 운영 요청 확장 예시**를 구분했다.
그 예시의 필드를 현재 YAML이나 `record()` 요청에 바로 추가하면 계약 검증에서 거부된다.

### 현재 호출과 입출력

알림은 Notifier, EHR/대시보드 표시는 별도 effect이며 두 모듈 모두
`record(request, recorded_at=...)`를 제공한다. 현재 native 실행은 기존
`chain-notification/v0.3`·`chain-effect/v0.3` 요청/receipt로 변환한다.

알림 호출부는 `chain/task_runtime.py:LocalTaskRuntime.invoke_work`다.
Notifier에 전달하는 request는 아래 6개 필드만 허용한다.

| 필드 | 현재 값/의미 |
|---|---|
| `request_schema` | `chain-notification/v0.3` |
| `request_id` | 작업의 `execution_id`. 기술 재시도에도 같은 값 사용 |
| `episode_id` | 알림 대상 Episode |
| `template` | Clinical 작업이 선택한 알림 코드. 본문 템플릿 자체가 아님 |
| `channel` | Site가 연결한 `CONSOLE/LOG` |
| `recipient` | Site의 `address_ref`. 현재 `mock:...` 주소 |

엔진의 원래 `chain-fact-work-request/v1`에는 Snapshot·identity·근거/확인 ID가 있지만,
위 변환에서 Notifier에는 넘기지 않는다. 본문·환자/내원 식별·Snapshot을 API에 제공하려면
명시적으로 승인한 필요한 항목만 새 계약으로 전달해야 한다.
본문에 사용할 항목은 작업의 Context/허용 범위에 선언하고 작업 준비 Snapshot에서 구성한다.
미확보 값을 추정하거나, 발송 시 별도 최신 자료로 준비된 메시지의 의미를 바꾸지 않는다.

Mock receipt의 필수 필드는 `receipt_schema/request_id/episode_id/payload_hash/template/channel/recipient/status/recorded_at`이다.
schema는 `chain-notification-receipt/v0.3`, status는 `MOCK_RECORDED`,
payload hash는 6필드 request의 canonical hash다. 요청과 receipt의 식별·종류·채널·대상이 일치해야 한다.
그 receipt를 공통 `chain-fact-work-result/v1`의 `receipt`로 감싸 반환한다.
현재 SUCCESS는 Mock 기록 성공이며 의료진 전달이나 수신 확인을 뜻하지 않는다.

### 실제 Provider를 연결할 때 확장할 곳

Clinical의 `work_rules/tasks`가 시점·종류·Context·수신 대상을 선택하고,
Site는 기관별 수신처, Safety는 허용 template, Deployment와 설치 목록은 Provider 구현을 선택한다.
현재 Clinical에는 본문/치환 문법이 없으므로 새 본문 YAML 필드를 임의로 추가하지 않는다.
본문 템플릿과 변수 검증·버전 고정을 지원할 명시적인 구현/Schema 확장이 필요하다.

운영 Provider는 `demo/providers.py:provider_factory`와 `demo/mocks/notifier.py`의 호출 형태를 참고해 별도 구현한다.
Worker 초기화에서 API client·인증을 제공하고 Deployment의 `notifier_binding` 및 승인 descriptor를 맞춘다.
현재 합성 factory는 `services["notifier_path"]`로 SQLite 경로를 받는다.
운영 Notifier API client의 서비스 key나 HTTP 주소는 현재 정의돼 있지 않다.

Provider 외에 다음 경계를 함께 수정해야 한다.

| 코드 | 확장할 내용 |
|---|---|
| `chain/task_runtime.py:LocalTaskRuntime.invoke_work` | Snapshot에서 허용된 본문/대상 정보를 변환하는 입력 계약과 실제 채널/receipt 처리 |
| `chain/tasks.py:TaskEngine._validate_receipt` | 현재 CONSOLE/LOG·MOCK_RECORDED 전용인 엔진 검증 |
| `chain/task_contracts.py`, authoring Schema·Registry | 새 작업/결과/설정 항목과 설치 모듈의 지원 범위 검증 |

백엔드는 요청 ID·메시지 추적 ID·해석한 수신 대상·실제 처리 단계·오류를 반환하도록 계약을 정한다.
API 접수와 발송·전달·수신 확인은 구분한다. timeout으로 응답을 못 받았다고 실제 발송도 없었다고 추정하지 않는다.
동일 요청을 확인/재시도할 때 같은 ID·내용을 유지하고, 바뀐 내용은 별도 요청으로 추적한다.
전달 상태 callback/조회와 엔진 반영 경로는 현재 제공하지 않으며 새 계약과 검증이 필요하다.

기술 재시도의 외부 request_id는 작업 execution_id로 유지한다.
Mock은 SQLite의 동일 request_id+payload hash를 확인해 기존 receipt를 반환하고 다른 payload의 ID 재사용은 거부한다.
effect는 증가하는 `effect_version`으로 늦은 활성화가 최신 해제를 덮어쓰지 못하게 한다.
로그가 반복 출력될 수 있으며 재시도만으로 모든 외부 효과의 정확히 한 번 실행을 보장하지 않는다.

현재 Notifier는 CONSOLE/LOG와 `MOCK_RECORDED` receipt만 수용한다.
실제 SMS/API 발송·EHR 변경·알림 회수는 수행하지 않는다.
실제 전달을 붙일 때에는 provider 외에도 전달/실패/중복 응답과 외부 idempotency 계약을 정하고
현재 Mock 전용 receipt 검증 경계를 확장해야 한다. 설정의 backend 이름 변경만으로 연결되지 않는다.
알림 경로는 CHAIN → 백엔드 Notifier API → 병원 알림 시스템/프론트엔드로 구성하며,
원천자료용 FHIR 조회 경로와 구분한다. 취소 안내와 표시 해제는 별도 작업이며 기존 알림을 삭제하거나 회수한 것으로 처리하지 않는다.

## 8. 저장소와 실행 환경을 준비한다

| 구성요소 | 현재 역할 / 통합 경계 |
|---|---|
| Temporal Service의 저장소 | Workflow History·Activity/Signal/Timer 등을 지속 보관하는 실행 기반. 구체 DB 배치는 Service 운영 영역 |
| Episode 원천/작업 상태 | Workflow 변수와 기록된 입력/Activity 결과로 복원. FactLedger의 Python 객체는 별도 PostgreSQL DB가 아님 |
| Agent Snapshot | 현재 전체 JSON을 Activity에 inline 전달. 외부 Snapshot 저장소 URL/reference 계약은 미구현 |
| Mock SQLite | 데모 `notices.sqlite`와 `effects.sqlite`에 중복 방지 receipt·표시 projection을 보관 |
| 병원 원천 저장소 | 실제 API/버전/정정·삭제 조회는 외부 Source 모듈에서 연결. 데모는 이미 공개된 합성 자료만 보관 |
| 통합 백엔드의 DB·메시지 큐 | 이 프로젝트에 애플리케이션 PostgreSQL·Kafka/Redis/RabbitMQ 연결 구현 없음. 필요한 운영 구성은 통합 측에서 결정 |

Temporal Task Queue는 사용 중이지만 별도의 병원 메시지 브로커를 이미 연결한 것은 아니다.
현재 Workflow/Activity 입력·결과로 원천 completion·Snapshot·열람 원문이 들어갈 수 있어 Temporal History에도 자료가 남는다.
운영의 접근권한·보존·암호화·필요 시 외부 Snapshot 참조 설계는 추가 통합 범위다.
Mock 발송을 실제 API로 교체한다고 중복 방지·감사 저장 책임이 자동으로 없어지지는 않는다.

기본 실행은 Python 3.11 이상(3.12 권장), [requirements.txt](../../requirements.txt)의 Temporal SDK·PyYAML,
연결 가능한 Temporal Service와 같은 namespace/Artifact Queue의 Worker가 필요하다.
추가 병원/Agent SDK·자격정보·네트워크·API timeout은 모듈/배포 환경에서 준비한다.
설치 inventory는 신뢰된 승인 입력이며 sandbox나 전이 의존성 자동 승인 기능이 아니다.
외부 호출은 모듈 자체의 timeout/취소·idempotency도 필요하다. Python thread 대기 timeout이 실제 외부 호출을 강제 종료하지 않는다.

이 안내는 현재 코드 계약의 연동 위치를 설명한다. 실제 병원 API·사용자 인증·외부 전달·운영 DB/화면을
통합해 검증한 완료 보고로 사용하지 않는다.
