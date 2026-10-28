# 사실 기반 YAML 작성 항목 사전

실제 형식 기준은 [제품 Schema](../../chain/resources/authoring-v1.schema.json),
참조·타입 검사는 `chain/semantic.py`, 실행 가능한 예시는 [config/minimum](../../config/minimum/)다.
[작성 순서](fact_authoring.md)를 먼저 읽고 필요한 항목을 이 문서에서 찾는다.
아래 표는 새 문법을 제안하지 않고 현재 지원 항목을 정리한다.

## 공통 형식

| 항목 | 현재 규칙 |
|---|---|
| 문서 형식 | `chain-clinical/v1`, `chain-site/v1`, `chain-safety/v1`, `chain-deployment/v1`, `chain-module-metadata/v1` |
| 내용 버전 | 각 `version` 문자열. 문서 `schema`/실행 `contract_version`과 구분 |
| 공통 실행 의미 | Clinical의 `contract_version: chain-fact-execution/v1` |
| ID | 영문자 또는 `_`로 시작. 이후 영문·숫자·`_`·`-`. 의미가 바뀐 임상 ID를 재사용하지 않음 |
| YAML 순서 | mapping key 순서에 실행 의미 없음. 숫자 `priority`와 `after` 등 명시 선언을 사용 |
| 빈 항목 | 빈 값이 허용되는 필수 mapping/list는 `{}`/`[]`로 명시. 모든 구역이 빈 값을 허용하는 것은 아님 |
| 오류 처리 | 알 수 없는 필드/버전·중복 key·없는 참조·금지된 형식은 거부 |
| 파일 제한 | 문서당 2 MiB, JSON 구조 깊이96/노드100,000. merge key·비문자 key·비유한 수·순환 alias 등 거부 |

Clinical 최상위 `origin_refs`의 각 출처는 `path`, `anchor`, `status`를 가진다.
`path`는 상대적인 출처 경로, `anchor`는 문서의 절/식별자다.
status는 `existing-approved`, `wg1-review`, `synthetic-test` 중 하나다.
이 표시는 추적 정보이며 승인 발급·서명 검증이나 경로별 임상 활성화를 대신하지 않는다.

## Clinical 전체 항목

| 구역 | 필수 내용 |
|---|---|
| 식별 | `schema`, `spec_id`, `version`, `profile`, `contract_version` |
| `context_service` | 구조화 역할의 `agent`, `mode` |
| `origin_refs` | 출처 ID→`path/anchor/status` |
| `facts` | Fact ID→`type/source/origin_refs` |
| `contexts` | 범위 ID→`fields/summary/origin_refs` |
| `milestones` | 현재 계산 또는 최초 수락 캡처 |
| `states` | State ID→13항목. 세부 항목은 [13항목 표](fact_authoring.md#4-임상팀의-13개-state-명세를-채운다) |
| `state_projection` | `fallback`, 규칙 ID별 `state/priority/when/requirement_refs/origin_refs` |
| `tasks` | 작업 종류별 기능·식별·입력·선행 결과 |
| `work_rules` | 규칙 ID별 `task/when/stop_when/origin_refs` |
| `decisions` | 결정 ID별 `question/options/evidence_context/requirement_ref/origin_refs` |
| `timers` | 임상 Timer ID별 소유/시간/만료/취소 계약 |
| `lifecycle` | `initial/states/projection/completion/origin_refs` |

`profile`은 현재 `minimum-demo` 또는 `extension-review`다.
`extension-review`는 Safety의 `purpose: review-only`와 함께 사용하는 검토 형식이다.
Schema로 표현 가능하다는 것만으로 임상 프로필의 실행 승인이 되지는 않는다.

### facts·contexts·milestones

| 항목 | 형식 / 의미 |
|---|---|
| `facts.*.type` | `string`, `number`, `integer`, `boolean`, `object` |
| `facts.*.source` | `initial` 또는 `external`. initial은 생성 정보8필드만 |
| `contexts.*.fields` | 선언한 Fact ID의 중복 없는 목록 |
| `contexts.*.summary` | `true`이면 이 범위의 구조화 Summary를 준비 |
| 현재 마일스톤 | `{when: <조건>, origin_refs: [...]}` |
| 최초 수락 캡처 | `{capture_first: {ref: results.<작업>.<필드>}, type: <기본타입>, origin_refs: [...]}` |

캡처 type은 `string/number/integer/boolean`이다. `when`과 `capture_first`를 한 항목에 함께 쓰지 않는다.
현재 마일스톤의 의존성은 순환할 수 없다. 캡처는 현재 입력의 수락된 성공 결과만 사용하며
이전 입력 참고 결과·실패 결과를 첫 정답으로 기록하지 않는다.

## 조건과 참조

### 참조 이름

| 형식 | 가리키는 내용 |
|---|---|
| `facts.<Fact ID>` | 현재 유효 원천/초기 정보 |
| `milestones.<마일스톤 ID>` | 계산한 현재 값 또는 선언한 최초 수락 이력 |
| `results.<Agent 작업 ID>.<출력 필드>` | 해당 작업의 현재 입력에 유효한 수락 결과 |
| `decisions.<결정 ID>.value` | 검증·수락한 현재 결정 값 |
| `state_entries.<State ID>.entered` | 기록된 State 진입 사실 |
| `state_entries.<State ID>.generation` | 해당 State의 진입 세대 |

`results` 뒤에는 논리 Agent 이름이 아니라 **tasks의 작업 ID**를 쓴다.
현재 State를 읽는 `state.S1`, 임의 중첩 JSON 탐색, Python `eval`은 지원하지 않는다.
결과 필드는 해당 Agent mode의 `output_fields`에 선언돼 있어야 한다.

### 연산 양식

```yaml
# 알려진 값 비교
eq: [{ref: facts.ct_order_id}, {ref: facts.ncct_order_id}]
```

| 연산 | 값의 형식 |
|---|---|
| `all` / `any` | 비어 있지 않은 조건 목록 |
| `not` | 조건 하나 |
| `eq` / `ne` | 피연산자 두 개. 호환 타입의 알려진 값 비교 |
| `gte` | 숫자 피연산자 두 개. bool을 숫자로 취급하지 않음 |
| `in` | 값과 `{literal: [...]}` 목록 |
| `is_known` | `{ref: facts.<ID>}` |
| `has_status` | `{ref: facts.<ID>, status: UNKNOWN}` 등 |
| `requirement` | Safety의 `kind: predicate` 요건 ID |

값의 피연산자는 `{ref: ...}` 또는 `{literal: ...}`다.
`is_known`은 KNOWN일 때 TRUE, ABSENT_CONFIRMED일 때 FALSE이며 미확보·오류 등은 UNKNOWN이다.
UNKNOWN 비교와 `not UNKNOWN`은 UNKNOWN이다. `has_status`는 상태 자체를 확인할 때 사용한다.

상태 이름은 `KNOWN`, `ABSENT_CONFIRMED`, `UNKNOWN`, `PENDING`, `CONFLICT`, `ERROR`, `RETRACTED`, `INVALIDATED`다.
특정 원천의 `NO_EVIDENCE`를 읽었다고 임상 부재가 확인된 것으로 바꾸지 않는다.
원천 상태의 정규화와 임상 조건은 구분한다.

### 투영 규칙

`priority`는 0 이상의 정수이며 같은 projection 안에서 중복될 수 없다.
TRUE인 `when`과 참조한 predicate 요건을 모두 만족한 후보 중 가장 큰 priority를 선택한다.
규칙의 State는 선언돼 있어야 한다. `fallback`도 선언된 State를 지정한다.
더 높은 후보의 UNKNOWN과 현재 선택 근거는 Query의 projection에 따로 표시된다.

## 작업과 실행 규칙

모든 task는 `kind`, `identity`, `after`, `completion_effect`, `origin_refs`를 가진다.

| kind | 기능별 필수 필드 |
|---|---|
| `agent` | `agent`, `mode`, `context` |
| `hitl` | `decision` |
| `notify` | `template`, `recipient`, `context` |
| `effect` | `operation`, `resource`, `target`, `context` |

`identity`는 `{subject: episode, repeat: ...}` 또는
`{subject: cause, subject_ref: <허용 참조>, repeat: ...}`다.
repeat는 `once/input_change/condition_reentry/cause_change` 중 하나다.
`subject_ref`는 원인 대상을 식별한다. 대상을 알 수 없으면 임의 대상을 선택하지 않는다.

`after`는 현재 성공 결과를 기다릴 Agent task ID 목록이다. 다른 task kind를 선행 결과로 쓰는 것은 현재 거부한다.
`after` 및 결과를 참조하는 실행 조건의 순환도 허용하지 않는다.
`work_rules`의 한 규칙은 작업 ID 하나를 가리키며 같은 task에 여러 규칙을 연결하지 않는다.
허용된 시작 `when`이 TRUE이고 `stop_when`이 TRUE가 아닐 때 실행 후보가 된다.

`completion_effect: true`는 notify/effect에서만 사용할 수 있다.
종료 처리 차례의 허용된 알림/표시를 소유 작업 집합에 넣는 선언이며
Agent/HITL을 계속 추가하거나 종료를 무한히 지연시키는 옵션이 아니다.

## 결정과 Timer

### decisions

| 필드 | 의미 |
|---|---|
| `question` | 의료진에게 보여줄 질문 |
| `options` | 코드별 `{effect: commit}` 또는 `{effect: defer, resume_timer: ...}` |
| `evidence_context` | 근거로 준비할 Context ID |
| `requirement_ref` | Safety의 `kind: hitl` 요건 ID |
| `origin_refs` | 원본 출처 |

`commit`은 결정 사실을 수락한다. 처방·투약·오더를 실행한다는 뜻이 아니다.
`defer`는 확정하지 않고 보류한다. `resume_timer`는 해당 결정에 연결된 재질문 Timer여야 한다.
선택별 필수 확인은 Safety에서 정의하며 존재하지 않는 선택 코드의 확인사항을 쓰지 않는다.

### timers

| 필드 | 허용값 / 의미 |
|---|---|
| `owner` | `{kind: state, id: <State>}` 또는 `{kind: decision, id: <결정>}` |
| `anchor` | State 소유자는 `state_entered`; 결정 소유자는 `decision_opened/decision_deferred` |
| `after` | `{value: <양수>, unit: s|min|h}` |
| `on_expire` | `{kind: task, id: <task>}` 또는 `{kind: resume_decision, id: <결정>}` |
| `cancel_when` | typed 조건 |
| `reset_on` | `new_owner_generation` |
| `repeat` | `{max_firings: 1}` |
| `origin_refs` | 시간값/동작의 출처 |

단순 정보 수신으로 Timer를 무한 재설정하지 않는다. 새 State 체류/질문 주기는 새 소유 세대로 구분한다.
늦게 도착한 옛 Timer가 현재 보류를 해제하지 못하도록 ID·세대를 검사한다.

### lifecycle

현재 승인된 실행은 `initial: ACTIVE`, `states: [ACTIVE, COMPLETED]`와 그 투영/완료 계약을 사용한다.
`completion`은 `{state: COMPLETED, wait_for: owned_work, after_completion: reject}`다.
완료 조건과 유한한 종료 작업 정리를 구분하고, 완료 뒤 Event는 거부한다.
휴면·행정 마감·재개 이름을 Schema가 표현할 수 있어도 해당 프로필의 실행을 승인한 것은 아니다.

## Site

식별 `schema/site_id/version` 외 아래 구역을 작성한다.

| 구역 | 형식 / 연결 |
|---|---|
| `sources` | 논리 Source ID→`system/binding` |
| `fact_bindings` | 외부 Fact ID→`source/field` 목록. 선택적으로 `record_type` |
| `role_bindings` | 표준 역할명을 같은 이름에 연결하는 identity 선언. 실제 인증·외부 역할 변환은 백엔드 책임 |
| `recipients` | 논리 수신처 ID→`channel/address_ref` |
| `effect_targets` | 논리 표시 대상 ID→설치 `binding` |

현재 데모의 실제 필드 연결 예시:

```yaml
fact_bindings:
  initial_note:
  - source: emr
    field: /raw/document
    record_type: ED_INITIAL_NOTE
```

`demo/source_store.py`는 이 pointer와 기록 종류를 읽어 공개된 원천에서 해당 항목을 투영한다.
이는 데모의 정규화 자료 형식이다. 다른 병원 API에 같은 pointer가 존재한다고 가정하지 않는다.
실제 Source 모듈이 해당 필드를 제공해야 하며 새로운 mapping만 썼다고 API 조회가 생기지는 않는다.
복수 출처 목록의 순서로 임상 우선순위를 정하지 않는다. 값·단위·유효성 충돌을 보존한다.

`role_bindings`에 외부 역할 별칭을 나열해 엔진이 자동 번역한다고 해석하지 않는다.
현재는 표준 역할 `X`를 `[X]`에 연결한 선언만 허용하며 다른 매핑은 `ROLE_BINDING_UNSUPPORTED`로 거부한다.
백엔드는 인증된 사용자의 역할을 합의한 표준 역할로 변환하고 `actor.role`을 전달해야 한다.
실제 인증/역할 발급 모듈은 현재 프로젝트에 연결되지 않았다.

## Safety

식별 `schema/policy_id/version/purpose` 외 아래 구역을 작성한다.

| 구역 | 형식 / 검사 |
|---|---|
| `requirements` | predicate: `kind/when/origin_refs`; hitl: `kind/roles/confirmations/origin_refs` |
| `agent_approvals` | Agent ID별 허용 `versions/actions/data_scopes` |
| `query_approvals` | Source ID별 허용 `fields/data_scopes` |
| `forbidden_actions` | 허용하지 않는 행동 코드 목록 |
| `notification_templates` | 사용할 수 있는 알림 template ID 목록 |
| `allowed_effect_operations` | 허용된 표시 operation 목록 |

`confirmations`는 선택 코드→확인항목 목록이다. 현재 합성 예시의 네 임상 확인사항은 기존 최소 흐름을 보존한 것이다.
새 확인항목을 임의로 추가하거나 Source 조회 성공을 새로운 필수 임상 Gate로 삼지 않는다.
`purpose: synthetic-draft`와 모의 역할 문자열 검사는 병원 운영 승인/사용자 인증을 뜻하지 않는다.

## Deployment와 모듈 제공 정보

식별 `schema/deployment_id/version/site_id` 외 아래 구역을 작성한다.

| 구역 | 항목 |
|---|---|
| `agents` | Agent ID별 `implementation_ref/version/timeout_s/backend` |
| `backend` | `kind: fixture/structured/api`, 필요한 경우 `config_ref` |
| `source_bindings` | Site의 Source binding→설치 구현 참조 |
| `notifier_binding` / `effect_binding` | 설치된 알림/표시 구현 참조 |
| `activity` | 양수 `timeout_s`, `maximum_attempts` 1~10 |
| `preparation` | 양수 `timeout_s`, `maximum_attempts` 1~10, `maximum_pages` 1 이상, `maximum_rounds` 1~10 |
| `reconciliation` | `on_startup: true`, `on_reconnect: true`, 양수 `poll_interval_s` |

시작/재연결 보정의 false는 현재 Schema에서 거부한다.
임상 Timer 시간은 이 파일에 넣지 않는다. backend kind만 `api`로 바꿔도 실제 API 구현이 생기지 않는다.
개별 Agent의 timeout과 전체 Activity/준비 예산은 별개이며, timeout은 외부 thread의 강제 종료를 보장하지 않는다.

`module_metadata.json`의 각 Agent는 `version`, `request_contract`, `result_contract`,
`modes`, `requires_summary`, `actions`, `data_scopes`를 제공한다.
각 mode는 `output_fields`를 선언한다. Clinical의 Agent ID/mode와 실제 모듈 계약이 일치해야 한다.

`installed.json`의 각 구현은 `kind/version/entrypoint/files/metadata/status/approval_ref/capabilities`를 가진다.
개발·배포 담당이 실제 코드/검증 callback을 구현하고 승인 목록과 바이트 해시를 관리한다.
이 파일을 임상 조건의 추가 입력 파일로 사용하거나, 해시를 수동 조정해 오류를 피하지 않는다.
새 primitive·외부 I/O·승인은 [연동 계약](fact_integration.md), Artifact는 [운영 안내](fact_operations.md)를 따른다.

## 오류를 읽는 방법

진단은 파일, 항목 경로, 오류 코드와 원인을 표시한다. 예를 들어 `clinical.tasks.<ID>.mode`이면
그 작업에서 선택한 mode를 먼저 확인한다. 앞쪽에서 하나를 고친 뒤 다시 검증해 남은 오류를 확인한다.

| 대표 코드 | 확인할 곳 |
|---|---|
| `DOCUMENT_READ_FAILED` / `DOCUMENT_PARSE_FAILED` | 경로/UTF-8·YAML 들여쓰기·문법 |
| `DUPLICATE_KEY` / `NON_STRING_KEY` | 같은 키 반복·merge key·숫자/비문자 키 |
| `SHAPE_INVALID` / `SCHEMA_VERSION_UNSUPPORTED` | 누락/허용하지 않는 필드·잘못된 값·신구 형식 혼합 |
| `UNDEFINED_REFERENCE` | Fact/Context/task/State/결정/요건/Timer ID 오타 또는 누락 |
| `CONDITION_TYPE_MISMATCH` / `UNSUPPORTED_REFERENCE` | 비교 타입·허용 namespace/결과 필드 |
| `DUPLICATE_PRIORITY` / `DEPENDENCY_CYCLE` | 우선순위 중복·파생/선행 관계 순환 |
| `PREDECESSOR_CONTRACT_MISMATCH` | `after`가 Agent 결과 외 작업을 가리키는지 |
| `ROLE_BINDING_UNSUPPORTED` | Site의 역할 연결에 현재 지원하지 않는 별칭/변환을 넣었는지 |
| `AMBIGUOUS_WORK_RULE_TARGET` | 같은 task를 여러 work rule에서 사용했는지 |
| `LIFECYCLE_APPROVAL_REQUIRED` | 현재 실행 범위 밖의 수명 프로필을 활성화하려 했는지 |
| `INSTALLATION_HASH_MISMATCH` / `WORKER_IMPLEMENTATION_MISMATCH` | 배포한 실제 파일과 승인/실행 Artifact가 같은지 |
| `FIXTURE_NOT_FOUND` / `FIXTURE_AMBIGUOUS` | 합성 Agent 입력과 고정 fixture가 정확히 맞고 하나만 선택되는지 |

컴파일 성공은 실제 API·Agent 결과·임상 적합성의 성공 증거가 아니다.
변경한 흐름을 실제 입력/결과·실패/경합·해당 Local/Temporal 시험에서 확인한 뒤 새 실행에 적용한다.
