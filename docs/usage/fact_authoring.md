# 사실 기반 Workflow 작성 안내

이 문서는 `chain-clinical/v1` 형식의 YAML을 작성·수정하는 방법을 설명한다.
실행 가능한 합성 예시는 [config/minimum](../../config/minimum/)에 있다.
v0.4는 Clinical/Site/Safety/Deployment 형식만 받는다. 구 raw/State-spec YAML과 `legacy-registry:` 구현 참조는 명시적으로 거부한다.

먼저 [데모 실행 안내](../../demo/README.md)로 현재 실행을 확인하고,
필드별 허용값은 [작성 항목 사전](fact_authoring_reference.md)을 참고한다.
여기 있는 합성 시간값과 분기는 운영 임상 기준을 새로 승인하는 예시가 아니다.

## 1. 무엇을 바꾸려는지 먼저 정한다

| 하려는 작업 | 고칠 위치 | 함께 확인할 내용 |
|---|---|---|
| State 설명·자료 표시 항목을 바꾼다 | Clinical의 `states` | 설명과 자료 표시가 실행 조건을 바꾸지는 않음 |
| 현재 환자 위치를 계산하는 조건을 바꾼다 | Clinical의 `state_projection`·`milestones` | 우선순위·Safety 참조·UNKNOWN일 때의 결과 |
| Agent·HITL·알림·표시 작업을 시작/중단한다 | Clinical의 `tasks`·`work_rules` | 입력 범위·선행 결과·반복 기준·허용 권한 |
| 질문·선택·판단 근거를 바꾼다 | Clinical의 `decisions`·`contexts` | 역할·필수 확인은 Safety, 화면의 근거 개정 계약은 유지 |
| 임상 대기·보류 재질문 시간을 바꾼다 | Clinical의 `timers` | 기준 사건·취소 조건·만료 작업도 함께 확인 |
| 병원 자료의 출처/필드를 연결한다 | Site의 `sources`·`fact_bindings` | Source 모듈의 실제 응답과 Safety의 조회 허용 범위 |
| 역할·필수 확인·금지 행동·허용 범위를 바꾼다 | Safety | 독립 승인 사항. Clinical에서 우회하지 않음 |
| 구현 모듈·버전·기술 제한을 바꾼다 | Deployment | 설치 목록·모듈 계약·승인된 파일 해시 |

업무 규칙의 원본은 Clinical이다. Site에는 병원 연결을, Safety에는 독립 승인 상한을,
Deployment에는 설치 구현과 기술 제한을 작성한다. 같은 질문·전이·시간값을 여러 파일에 복사하지 않는다.
모듈이 제공하는 기능, 임상 규칙이 요청하는 기능, 정책이 허용하는 기능을 대조하는 검사는 유지한다.

## 2. 파일을 구분한다

| 파일 | 작성할 내용 |
|---|---|
| `clinical.yaml` | 사실, 입력 범위, 마일스톤, State 명세와 표시 계산, 작업, 결정, 임상 타이머, 최소 수명 |
| `site.yaml` | 논리 출처와 병원 시스템/필드 연결, 표준 역할명, 알림 수신처, 표시 대상 |
| `safety.yaml` | 안전 조건, HITL 역할·확인사항, Agent/조회 승인, 금지 행동, 허용 알림/표시 작업 |
| `deployment.yaml` | 승인된 구현 참조·버전·backend, Activity/준비/보정의 기술 제한 |
| `module_metadata.json` | 모듈 제공자가 정한 Request/Result·mode·출력 필드·행동/자료 범위·Summary 의존 여부 |
| `installed.json` | 배포 담당이 관리하는 승인 모듈 목록·진입 함수·파일 바이트 해시·capability |

임상 작성자는 Python 진입 경로나 SHA 해시를 직접 맞추지 않는다.
새 Agent나 Source를 연결할 때에는 구현 코드·계약·승인·배포가 추가로 필요하다.
해시를 계산한 것만으로 모듈이 승인되거나 실제 병원 API가 연결되는 것은 아니다.

파일 이름보다 각 문서의 `schema`가 형식을 결정한다. 내용의 `version`과
공통 실행 의미인 `contract_version: chain-fact-execution/v1`도 구분한다.
적용 시에는 파일들을 함께 검증하고 한 실행에 고정한다.

## 3. Clinical은 사실·표시 위치·실행 작업을 나눠 작성한다

```text
공개된 원천자료 → facts → milestones → state_projection → 주 State 표시
                        └→ work_rules → tasks → Agent/HITL/알림/표시 작업
```

주 State 하나에 모든 작업을 넣지 않는다. 예를 들어 검사결과가 들어왔다는 사실은 보존하고,
그 사실을 사용할 작업이 현재 필요한지는 작업 조건에서 따로 판단한다.
State 진입 자체를 기준으로 삼을 때는 `state_entries`의 진입 사실/세대를 참조할 수 있다.

### 사실과 입력 범위

`facts`는 어떤 항목을 받을지 선언한다. 환자 값이나 검사 정답을 채우는 곳이 아니다.
생성 시점의 `initial` 항목은 아래 8개뿐이다.

`site_id`, `patient_id`, `encounter_id`, `episode_id`, `age`, `sex`, `bed`, `arrival_time`

초진기록·검사·처방 등은 `source: external`로 선언한다.
과거에 발생한 사실이라도 엔진에 나중에 유입됐다면 수신·적용 이후부터 사용한다.

아래는 현재 Clinical의 일부다.

```yaml
facts:
  initial_note:
    type: object
    source: external
    origin_refs: [legacy-flow]
contexts:
  screening_input:
    fields: [initial_note, lkw, glucose]
    summary: true
    origin_refs: [legacy-flow]
```

`contexts`는 한 작업에 제공할 항목의 범위다. `summary: true`이면 해당 범위의 현재 확보 자료를
구조화 Summary로 준비한다. Episode 전체를 매번 요약하거나 미확보 값을 정상값으로 채우지 않는다.
현재 Summary는 구조화 자료를 조합하는 구현이며 자연어 추출/NLP를 구현한 것이 아니다.

### 마일스톤과 현재 위치

`milestones`에는 현재 사실로 다시 계산하는 조건과 최초 수락 결과의 역사적 캡처가 있다.
`when`은 현재 자료가 바뀌면 재계산한다. `capture_first`는 처음 수락한 결과를 보존한다.
둘을 구분해야 정정된 최신 자료와 과거 경로 개시 사실을 함께 유지할 수 있다.

`state_projection.rules`는 조건이 TRUE인 후보 중 `priority`가 가장 큰 State를 표시한다.
같은 우선순위는 오류다. YAML에 먼저 썼는지, State 번호가 큰지는 의미가 없다.
아무 후보도 성립하지 않으면 `fallback`을 표시하고 미확정 근거를 함께 남긴다.
관련 사실이 철회되면 이전 위치로 다시 계산될 수 있다. 과거 결정이나 외부 치료를 자동 취소하는 뜻은 아니다.

## 4. 임상팀의 13개 State 명세를 채운다

현재 양식은 아래 항목명을 그대로 사용한다. 다른 표기·필드를 덧붙이면 검증에서 거부할 수 있다.
`states`의 key가 실행 ID이며 `ST-ED-010`처럼 임상 State ID를 사용한다.
`State.name`은 표시 이름, `clinical_ids`는 관련 임상 ID 목록이다.
`state_projection`의 대상·fallback, Timer의 State 소유자, `state_entries` 참조에는 표시 이름이 아니라 실행 ID를 쓴다.
현재 최소 프로필의 `S3N`은 IVT 미시행 분기를 보존하기 위한 임시 ID이며 확장 임상 State와 구분한다.

| 명세 항목 | 작성 내용 |
|---|---|
| State | 표시 이름 `name`, 관련 임상 ID 목록 `clinical_ids` |
| Clinical Goal and Scope | 목표 `goal`, 시작 범위 `start`, 종료 범위 `end` 설명 |
| Entry Trigger | 연결할 표시 계산 규칙 `projection_rule_refs`, 설명 |
| Incoming Data / Evidence | 연결할 입력 범위 `context_refs`, 설명 |
| Data requirement | Fact·MANDATORY/OPTIONAL/CONDITIONAL·설명·필요 시 표시용 freshness |
| Required Capability | 관련 작업의 `task_refs` |
| Agent Input | `{derive: true}`. 작업과 Context에서 생성 |
| Expected Agent Output | `{derive: true}`. 모듈 계약에서 생성 |
| HITL | `{derive: true}`. 결정과 Safety에서 생성 |
| Guard / Transition condition | `{derive: true}`. typed 조건과 Safety에서 생성 |
| Next State / Branch | `{derive: true}`. 표시 계산과 결정에서 생성 |
| Timer / Exception / Fail-safe | `timer_refs`, 설명 `notes` |
| Safety / Audit | 적용 요건 `requirement_refs`, 출처 `origin_refs` |

`derive` 항목은 자동으로 채우는 연결 정보다. Agent 입력·선택지·다음 State를 여기에 다시 쓰지 않는다.
설명문이나 `Data requirement: MANDATORY`는 실행을 허용하는 조건으로 자동 적용되지 않는다.
실제 시작·중단·표시 조건은 아래 typed 조건에 작성한다.

## 5. 조건은 문장 대신 정해진 연산으로 작성한다

`ref`는 선언된 항목의 참조, `literal`은 비교할 실제 값이다.

```yaml
when:
  eq:
  - ref: decisions.H1.value
  - literal: PROCEED_TO_CT
```

위 조건은 H1에서 수락된 값이 PROCEED_TO_CT일 때 TRUE다.
`when: CT 진행에 동의한 경우`처럼 자연어를 쓰거나 Python 식을 넣을 수 없다.
현재 연산은 `all`, `any`, `not`, `eq`, `ne`, `gte`, `in`, `is_known`, `has_status`, `requirement`다.

미확보·충돌·철회 자료의 일반 비교는 UNKNOWN이다. UNKNOWN에 `not`을 붙여도 TRUE가 되지 않는다.
값이 알려진 경우의 비교와 자료 상태 자체의 확인을 구분한다.
세부 형식과 참조 목록은 [항목 사전](fact_authoring_reference.md#조건과-참조)을 따른다.

현재 NCCT 표시 규칙은 다음과 같다.

```yaml
to_ST-IMG-020:
  state: ST-IMG-020
  priority: 50
  when:
    eq: [{ref: facts.ncct_completed}, {literal: true}]
  requirement_refs: [CT_PATH_APPROVED, NCCT_ORDER_MATCH]
  origin_refs: [legacy-flow]
```

`ST-IMG-010`에서 `ST-IMG-020`을 표시하기 위한 추가 NCCT 조건은 해당 촬영 완료와 오더 일치뿐이다.
혈소판/INR 완비·영상 직접 판독·Agent final·`review_ready`를 이 전이 조건에 더하지 않는다.
final/Summary 결과는 다음 HITL 준비의 선행조건으로 별도로 다룬다.
이 예시의 임상 조건을 바꾸려면 해당 임상·정책 검토를 거친다.

## 6. 작업의 기능과 실행 조건을 연결한다

`tasks`는 무엇을 실행할지, `work_rules`는 어떤 사실에서 시작/중단할지 작성한다.
현재 `tpa_interim` 작업의 기능 선언은 다음과 같다.

```yaml
tpa_interim:
  kind: agent
  agent: tpa-decision-support-agent
  mode: interim
  context: tpa_input
  identity: {subject: episode, repeat: input_change}
  after: []
  completion_effect: false
  origin_refs: [legacy-flow]
```

이 블록은 `tasks` 아래에 놓는다. 같은 ID의 `work_rules`에는 CT 진행 승인 시 시작하고
해당 NCCT가 완료·일치하면 중단하는 조건이 있다.
두 구역의 ID가 같아도 서로 다른 사전이며 `work_rules.*.task`가 작업을 연결한다.

`after`에는 현재 입력에 유효한 성공 결과를 기다릴 **Agent 작업 ID**를 넣는다.
선행 결과가 없거나 이전 입력의 참고 결과이면 기다린다. `after: []`인 독립 작업은 병렬 준비가 가능하다.
같은 task를 여러 work rule이 동시에 지정하는 형식은 현재 거부한다. 원인이 다른 규칙은 작업 ID도 분리한다.

반복 기준은 다음과 같이 고른다.

| `identity.repeat` | 의미 |
|---|---|
| `once` | 같은 논리 대상·원인 주기에서 한 번 |
| `input_change` | 관련 입력/선행 결과가 바뀌면 새 실행 |
| `condition_reentry` | FALSE 뒤 TRUE가 되는 새 유효 주기 |
| `cause_change` | 명시한 원인/오더/Timer의 대상이 바뀌면 별도 작업 |

기술 재시도는 동일 요청 식별자를 유지한다. 관련 입력이 바뀐 새 평가는 새 실행으로 기록한다.
새로운 명령 종류를 만들거나 동적 반복·집계 등을 수행하려면 YAML 추가 외에 엔진 확장이 필요하다.

## 7. HITL과 자료 갱신 정책을 유지한다

`decisions`에는 질문, 선택지, 근거 Context, Safety 요건 ID를 작성한다.
작업이 질문을 필요로 하는 조건과 선행 Agent 결과는 `tasks`·`work_rules`에 작성한다.
역할과 선택별 필수 확인사항은 Safety의 `kind: hitl` 요건에서 관리한다.
선택지에 `next_state`를 중복 작성하지 않는다. 수락된 결정 사실을 State 투영이 참조한다.

실행 계약 `chain-fact-execution/v1`은 다음 정책을 공통으로 적용한다.

1. Agent·HITL·알림·표시 작업 직전에 필요한 원천자료를 재확인한다.
2. 실제 병렬 준비 묶음에서만 확인 결과를 공유한다. 순차 작업은 재조회한다.
3. 조회 실패는 현재 자료와 최신 여부 미확인 표시로 진행한다. 필수 Agent·Summary 실패를 성공으로 바꾸지는 않는다.
4. 실행 중 관련 입력이 바뀐 Agent는 기존 실행을 마친 뒤 최신 입력으로 재평가한다. 구결과는 참고용이다.
5. 열린 HITL의 관련 항목 갱신·추가는 같은 질문의 새 근거 개정으로 표시한다. 과거 Snapshot·원문은 보존한다.
6. 구개정 응답은 거부하고 최신 근거 확인 뒤 다시 응답한다. 먼저 수락한 결정은 나중 자료로 자동 취소하지 않는다.

HOLD/DEFER는 `effect: defer`와 `resume_timer`로 선언한다.
보류는 일반 자료 추가로 풀리지 않는다. 타이머 또는 공통 재요청으로 새 질문을 준비한다.
콘솔의 `ready`도 같은 백엔드 재요청 계약을 사용한다.

## 8. 임상 시간은 Clinical에서 수정한다

현재 `timers.H1_DEFER`의 일부다.

```yaml
H1_DEFER:
  owner: {kind: decision, id: H1}
  anchor: decision_deferred
  after: {value: 10, unit: min}
  on_expire: {kind: resume_decision, id: H1}
```

이 블록에는 `cancel_when`, `reset_on`, `repeat`, `origin_refs`도 필요하다.
기존 항목의 `after`만 고칠 때 다른 필드를 삭제하지 않는다.
단위는 `s`, `min`, `h`이며 시간값은 양수다. 현재 반복은 소유 주기당 `max_firings: 1`만 지원한다.
시간이 지났다는 이유로 자동 YES/NO나 State 전진을 만들지 않는다.

작은 수정 예로, 현재 10분을 같은 길이의 600초로 표현하려면 기존 `H1_DEFER`의 한 줄만 아래처럼 바꾼다.
다른 Timer 필드는 그대로 두고 9절의 명령으로 전체 패키지를 검증한다.

```yaml
after: {value: 600, unit: s}
```

이는 시간 표기의 변경 예다. 실제 대기시간을 바꾸려면 임상팀이 정한 값·단위와 출처를 함께 반영한다.
Site에는 임상 시간을 넣지 않는다. Agent 호출 제한·재시도·자료 준비 deadline은 Deployment,
Simulator 상대시간과 시연 가속은 데모 설정이다.
YAML을 저장해도 이미 실행 중인 Timer는 바뀌지 않는다. 검증한 새 설정으로 새 실행을 시작해야 한다.

## 9. 바꾼 설정을 검증하고 적용한다

먼저 실제 native 합성 패키지를 컴파일해 볼 수 있다. 모든 명령은 저장소 루트 기준이다.

```sh
.venv/bin/python -m chain config/minimum/clinical.yaml \
  --installations config/minimum/installed.json \
  --output-dir output/authoring-check-01
```

출력 디렉터리는 존재하지 않는 경로를 사용한다. 이 명령은 Schema·참조·설치/승인을 검사하고
공개 정의와 Manifest를 저장하며 Workflow·Worker·Agent를 실행하지 않는다.
성공 출력의 `scope: CONFIGURATION_INSPECTION`은 설정 검사의 범위를 뜻한다.
`bindings_validated: true`와 빈 `binding_blockers`를 확인하고,
실제 실행은 별도 진입점인 `chain.client.start_episode`에서 시작한다.
저장된 v1 Artifact에 남은 `runtime_ready: false`·`FACT_REDUCER_AND_EXECUTION_NOT_IMPLEMENTED`는
이전 검사 Artifact와의 호환 필드다. 현재 native 실행 여부를 이 필드 하나로 판정하지 않는다.
검토용 초안에서 `--validation-only`를 쓰는 것은 알려진 연결 공백을 blocker로 남기는 모드다.
문법·해시·승인 오류를 무시하거나 실행 승인으로 바꾸는 옵션은 아니다.

수정은 새 폴더에 복사한 파일에서 시작한다. Clinical을 다른 폴더에 놓고 Site/Safety/Deployment/
metadata를 함께 복사하지 않았다면 아래처럼 경로를 모두 지정한다.

```sh
.venv/bin/python -m chain work/my-clinical.yaml \
  --site config/minimum/site.yaml --safety config/minimum/safety.yaml \
  --deployment config/minimum/deployment.yaml \
  --metadata config/minimum/module_metadata.json \
  --installations config/minimum/installed.json \
  --output-dir output/authoring-check-02
```

필요한 변경을 검토·승인한 뒤 새 버전/Artifact로 배포한다. 기존 실행은 원래 Artifact를 유지한다.
State ID 변경도 새 구성과 Queue에서 시작하는 Episode에 적용하며 기존 실행·History의 ID를 다시 쓰지 않는다.
코어·모듈 파일을 덮어쓴 뒤 같은 Worker Queue로 계속 처리하는 방식은 지원하지 않는다.
모듈 구현은 [연동 계약](fact_integration.md), 설치·배포·버전 보존은 [운영 안내](fact_operations.md)를 따른다.

## 10. YAML 수정만으로 가능한 범위와 추가 작업

| 변경 | 추가로 필요한 것 |
|---|---|
| 기존 필드·모듈을 쓰는 설명/typed 조건·우선순위·지원 Timer 값 수정 | 참조 검증·임상/정책 검토·새 Artifact 배포 |
| 새 State/마일스톤/기존 Agent 호출 추가 | 13항목·투영/작업/조건·끊어진 참조 점검. 새로운 실행 primitive가 아니면 코어 수정 없이 작성 |
| 새 원천 Fact 추가 | 타입·Site 매핑·조회 승인·Source 계약/실제 응답·필요한 입력 fixture/Agent Schema |
| 새 Agent 추가 | 개별 구현·입출력 검증·metadata·독립 승인·Deployment·설치 해시·Worker 배포 |
| 새 알림/표시 방식 추가 | 모듈의 실제 기능과 수신처/대상·허용 정책. 현재 Mock만으로 실제 전달을 주장하지 않음 |
| 지원하지 않는 연산·동적 목록 작업·새 primitive·새 수명 동작 | 별도 실행 계약/엔진 구현·검증·해당 임상 승인 |

현재 합성 fixture는 정확한 입력에만 맞는다. Context 항목·값·버전을 바꾸면 컴파일은 통과해도
실행 시 `FIXTURE_NOT_FOUND`가 날 수 있다. 이는 맞는 가상 출력이 없다는 시험 오류이며 성공값으로 대체하지 않는다.

최소 프로필은 ACTIVE/COMPLETED 수명과 응급실 등록부터 IVT 시행 결정까지의 흐름·경로 종료 분기를 실행한다.
활성 State ID는 [데모의 State 목록](../../demo/README.md#state-식별자)을 따른다.
IVT 미시행의 `S3N`은 최소 데모 전용 임시 State로 유지하고 확장 임상 분기가 구체화되면 대체한다.
입원·전원·EVT 전체 여정, 종료 후 재개/재선별, 변경된 encounter 연결 등은 필요한 임상/연동 승인을 별도로 받는다.
`origin_refs.status: wg1-review`를 바꾸는 것만으로 승인이 생기지 않는다.
지원되지 않는 선언은 [오류 사전](fact_authoring_reference.md#오류를-읽는-방법)과 실제 진단을 확인한다.
