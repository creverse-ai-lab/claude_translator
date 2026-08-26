# 모델 비교 — 장문 실전 샘플

e2b(7.1GB)와 exaone3.5:2.4b(1.6GB)를 dispatch 파이프라인에서 같은 장문에 돌린 결과 원본입니다.
판단 포인트: 용어·말버릇 처리의 자연스러움, 원문 표현을 얼마나 존중하는지(유사어 교체 여부), 속도.


---

# 샘플: 저장소 분석 답변

Claude가 저장소 구조를 파악해 보고하는 전형적 답변. 표·코드·bullet·5절 초장문·Claude 말버릇(idempotent하지, flaky하고, import합니다) 포함. 1,537자.


## 1) 원문 (Claude 그대로)

조사해봤습니다. 이 저장소는 주문 처리 백엔드이고, 전체적으로 hexagonal architecture를 따르지만 일부 경계가 무너져 있습니다. 구조 파악 결과를 공유합니다.

## 전체 구조

| 디렉터리 | 역할 | 파일 수 |
|---|---|---|
| `src/domain/` | 핵심 비즈니스 로직. 외부 의존성 없음 | 42 |
| `src/adapters/` | DB, 외부 API, 메시지 큐 연동 | 31 |
| `src/api/` | REST 엔드포인트와 DTO | 18 |
| `src/batch/` | 정산 배치와 리포트 생성 | 9 |

진입점은 `src/api/server.ts`이고, 의존성 주입은 `src/container.ts`에서 수동으로 조립됩니다. DI 프레임워크는 사용하지 않습니다.

## 주요 흐름

주문 생성 요청이 들어오면 `OrderController`가 DTO를 검증하고, `CreateOrderUseCase`가 재고 확인과 결제 승인을 orchestrate하는데, 재고 확인은 동기 HTTP 호출이고 결제 승인은 큐를 통한 비동기 처리라서, 이 두 가지가 섞이면서 부분 실패 시나리오가 발생할 수 있고, 이를 보상 트랜잭션으로 처리하고 있습니다.

보상 로직은 `src/domain/saga/OrderSaga.ts`에 있습니다:

```typescript
// 결제 실패 시 재고를 원복한다
async compensate(order: Order): Promise<void> {
  await this.inventory.release(order.items);
  await this.orders.markFailed(order.id, "PAYMENT_REJECTED");
}
```

## 우려되는 부분

- `src/adapters/db/OrderRepo.ts`가 domain 레이어의 인터페이스를 우회해서 `OrderSaga`를 직접 import합니다. 경계 위반이고, 테스트에서 mock이 어려워지는 원인입니다.
- 재고 확인 HTTP 호출에 타임아웃이 없습니다. 상대 서비스가 느려지면 스레드가 무한정 대기합니다.
- `OrderSaga.compensate`가 idempotent하지 않아서 재시도 시 재고가 두 번 원복될 수 있습니다.
- 배치 코드는 테스트 커버리지가 0%이고, deprecated된 `moment.js`를 아직 사용합니다.
- 통합 테스트가 실제 DB에 의존해서 flaky하고, CI에서 간헐적으로 실패합니다.

## 권고

즉시 조치가 필요한 것은 타임아웃 추가입니다. 재고 확인 호출에 3초 타임아웃과 circuit breaker를 넣으세요. 그다음으로 compensate에 멱등성 키를 도입하는 것이 필요합니다. 경계 위반은 리팩터링 비용이 커서 다음 분기로 미뤄도 되지만, 새 코드가 같은 패턴을 따라하지 않게 lint rule을 추가해 두는 것이 권장됩니다.

테스트 전략은 별도 논의가 필요합니다. 통합 테스트를 testcontainers로 옮기면 flaky 문제가 해결될 것으로 보이는데, CI 실행 시간이 2배 늘어날 수 있어서 트레이드오프가 있습니다.


## 2) gemma4:e2b — 15.8초

이 저장소는 주문 처리 백엔드이고, 전체적으로 hexagonal architecture를 따르지만 일부 경계가 무너져 있습니다. 구조 파악 결과를 공유합니다.

## 전체 구조

| 디렉터리 | 역할 | 파일 수 |
|---|---|---|
| `src/domain/` | 핵심 비즈니스 로직. 외부 의존성 없음 | 42 |
| `src/adapters/` | DB, 외부 API, 메시지 큐 연동 | 31 |
| `src/api/` | REST 엔드포인트와 DTO | 18 |
| `src/batch/` | 정산 배치와 리포트 생성 | 9 |

진입점은 `src/api/server.ts`이고, 의존성 주입은 `src/container.ts`에서 수동으로 조립됩니다. DI 프레임워크는 사용하지 않습니다.

## 주요 흐름

주문 생성 요청이 들어오면 `OrderController`가 DTO를 검증하고, `CreateOrderUseCase`가 재고 확인과 결제 승인을 orchestrate합니다. 재고 확인은 동기 HTTP 호출이고 결제 승인은 큐를 통한 비동기 처리라서 이 두 가지가 섞이면서 부분 실패 시나리오가 발생할 수 있습니다. 따라서 이를 보상 트랜잭션으로 처리하고 있습니다.

보상 로직은 `src/domain/saga/OrderSaga.ts`에 있습니다:

```typescript
// 결제 실패 시 재고를 원복한다
async compensate(order: Order): Promise<void> {
  await this.inventory.release(order.items);
  await this.orders.markFailed(order.id, "PAYMENT_REJECTED");
}
```

## 우려되는 부분

- `src/adapters/db/OrderRepo.ts`가 domain 레이어의 인터페이스를 우회해서 `OrderSaga`를 직접 가져옵니다(import). 경계 위반이고, 테스트에서 mock이 어려워지는 원인입니다.
- 재고 확인 HTTP 호출에 타임아웃이 없습니다. 상대 서비스가 느려지면 스레드가 무한정 대기합니다.
- `OrderSaga.compensate`가 멱등적이지(idempotent) 않아서 재시도 시 재고가 두 번 원복될 수 있습니다.
- 배치 코드는 테스트 커버리지가 0%이고, deprecated된 `moment.js`를 아직 사용합니다.
- 통합 테스트가 실제 DB에 의존해서 불안정하고(flaky), CI에서 간헐적으로 실패합니다.

## 권고

즉시 조치가 필요한 것은 타임아웃 추가입니다. 재고 확인 호출에 3초 타임아웃과 circuit breaker를 넣으세요. 그다음으로 compensate에 멱등성 키를 도입하는 것이 필요합니다. 경계 위반은 리팩터링 비용이 커서 다음 분기로 미뤄도 되지만, 새 코드가 같은 패턴을 따라하지 않게 lint rule을 추가해 두는 것이 권장됩니다.

테스트 전략은 별도 논의가 필요합니다. 통합 테스트를 testcontainers로 옮기면 flaky 문제가 해결될 것으로 보이는데, CI 실행 시간이 2배 늘어날 수 있어서 트레이드오프가 있습니다.


---

**확인·조치 항목** (본문에서 그대로 모음)

- 그다음으로 compensate에 멱등성 키를 도입하는 것이 필요합니다.
- 테스트 전략은 별도 논의가 필요합니다.


## 3) exaone3.5:2.4b — 2.5초

이 저장소는 주문 처리 백엔드이고, 전체적으로 hexagonal architecture를 따르지만 일부 경계가 무너져 있습니다. 구조 파악 결과를 공유합니다.

## 전체 구조

| 디렉터리 | 역할 | 파일 수 |
|---|---|---|
| `src/domain/` | 핵심 비즈니스 로직. 외부 의존성 없음 | 42 |
| `src/adapters/` | DB, 외부 API, 메시지 큐 연동 | 31 |
| `src/api/` | REST 엔드포인트와 DTO | 18 |
| `src/batch/` | 정산 배치와 리포트 생성 | 9 |

진입점은 `src/api/server.ts`이고, 의존성 주입은 `src/container.ts`에서 수동으로 조립됩니다. DI 프레임워크는 사용하지 않습니다.

## 주요 흐름

주문 생성 요청이 들어오면 **`OrderController`**이 DTO를 검증합니다. **`CreateOrderUseCase`**은 재고 확인과 결제 승인을 조율하는데, 재고 확인은 동기 HTTP 호출 방식으로 진행되고, 결제 승인은 큐를 활용한 비동기 처리 방식을 취합니다. 이러한 방식으로 인해 부분 실패 시나리오가 발생할 가능성이 있으며, 이를 **보상 트랜잭션**으로 관리하고 있습니다.

보상 로직은 `src/domain/saga/OrderSaga.ts`에 있습니다:

```typescript
// 결제 실패 시 재고를 원복한다
async compensate(order: Order): Promise<void> {
  await this.inventory.release(order.items);
  await this.orders.markFailed(order.id, "PAYMENT_REJECTED");
}
```

## 우려되는 부분

- `src/adapters/db/OrderRepo.ts`가 domain 레이어의 인터페이스를 우회해서 `OrderSaga`를 직접 가져옵니다(import). 경계 위반이고, 테스트에서 mock이 어려워지는 원인입니다.
- 재고 확인 HTTP 호출에 타임아웃이 없습니다. 상대 서비스가 느려지면 스레드가 무한정 대기합니다.
- `OrderSaga.compensate`가 idempotent하지 않아서 재시도 시 재고가 두 번 원복될 수 있습니다.
- 배치 코드는 테스트 커버리지가 0%이고, deprecated된 `moment.js`를 아직 사용합니다.
- 통합 테스트가 실제 DB에 의존해서 불안정하고(flaky), CI에서 간헐적으로 실패합니다.

## 권고

즉시 조치가 필요한 것은 타임아웃 추가입니다. 재고 확인 호출에 3초 타임아웃과 circuit breaker를 넣으세요. 그다음으로 compensate에 멱등성 키를 도입하는 것이 필요합니다. 경계 위반은 리팩터링 비용이 커서 다음 분기로 미뤄도 되지만, 새 코드가 같은 패턴을 따라하지 않게 lint rule을 추가해 두는 것이 권장됩니다.

테스트 전략은 별도 논의가 필요합니다. 통합 테스트를 testcontainers로 옮기면 flaky 문제가 해결될 것으로 보이는데, CI 실행 시간이 2배 늘어날 수 있어서 트레이드오프가 있습니다.


---

**확인·조치 항목** (본문에서 그대로 모음)

- 그다음으로 compensate에 멱등성 키를 도입하는 것이 필요합니다.
- 테스트 전략은 별도 논의가 필요합니다.


---

# 샘플: 장애 원인 보고서

5KB급 장애 분석 보고서. 타임라인 표 7행, 코드 3블록, 번역투 다수(당신의, 보여집니다, 요구됩니다). 2,868자. 이 문서는 slang·문장 태스크가 없어 정규식 층+행동 항목 수집만 작동한 사례 — 두 모델의 출력이 동일합니다.


## 1) 원문 (Claude 그대로)

조사해봤습니다. 결론부터 말하면 결제 서비스 장애의 근본 원인은 세 가지가 겹친 복합 장애인 것으로 확인됩니다. 각각 단독으로는 문제가 되지 않았을 것이지만, 동시에 발생하면서 cascading failure가 트리거되었습니다.

## 타임라인

| 시각 | 이벤트 | 출처 |
|---|---|---|
| 02:13:05 | `payment-gateway` 레이턴시 p99가 180ms에서 2,400ms로 급증 | APM |
| 02:13:41 | `order-service` 스레드 풀 고갈 시작 (active 200/200) | 메트릭 |
| 02:14:02 | circuit breaker OPEN 전환, fallback 경로 활성화 | 앱 로그 |
| 02:14:33 | fallback 경로에서 NPE 다발, 에러율 34% 도달 | Sentry |
| 02:17:19 | 오토스케일러가 pod 8개에서 24개로 증설 | k8s 이벤트 |
| 02:19:05 | DB 커넥션 1,200개 도달, max_connections 초과 | RDS |
| 02:31:44 | 수동 롤백 완료, 에러율 정상화 | 배포 로그 |

## 근본 원인 1: 게이트웨이 레이턴시

당신의 인프라에서 우리는 02:13에 PG사 API의 응답 지연이 시작된 것을 발견했습니다. 이것은 외부 요인이라 통제가 불가능하지만, 문제는 타임아웃 설정이었습니다. `payment-gateway`의 HTTP 클라이언트 타임아웃이 30초로 설정되어 있었고, 이는 스레드가 최대 30초간 점유되는 것을 의미합니다. 업스트림 요청이 초당 40건인 상황에서 스레드 풀 200개가 고갈되는 데 걸리는 시간은 5초에 불과합니다.

설정은 다음과 같이 변경이 요구됩니다:

```yaml
payment:
  http-client:
    connect-timeout: 500ms
    read-timeout: 3s        # 기존 30s
    retry:
      max-attempts: 2
      backoff: 200ms
```

## 근본 원인 2: fallback 경로의 NPE

circuit breaker가 열린 후 fallback 메서드가 호출되었는데, 이 경로는 작년 11월 이후 한 번도 실행된 적이 없었던 것으로 보여집니다. 그 사이 `PaymentContext`에 `idempotencyKey` 필드가 추가되었고, fallback 경로에서는 이 필드의 초기화가 이루어지지 않았습니다.

```java
// PaymentFallbackHandler.java:47
public PaymentResult fallback(PaymentRequest req, Throwable t) {
    // req.getContext()가 null을 반환하는 경우가 처리되지 않았습니다
    String key = req.getContext().getIdempotencyKey(); // NPE 발생 지점
    return PaymentResult.pending(key);
}
```

테스트 커버리지를 확인해봤는데 fallback 경로의 커버리지는 0%였습니다. circuit breaker 관련 테스트가 존재하지 않는 것이 확인되었습니다.

## 근본 원인 3: 오토스케일러와 DB 커넥션의 상호작용

가장 흥미로운 부분입니다. 오토스케일러가 CPU 사용률을 기준으로 pod을 8개에서 24개로 늘렸는데, 각 pod은 커넥션 풀 50개를 가지고 있습니다. 24개 pod이면 최대 1,200개의 커넥션이 생성되는데, RDS의 max_connections는 1,000입니다. 즉 스케일 아웃이 오히려 DB를 죽이는 구조였던 것입니다. 레이턴시 문제로 CPU가 올라간 것인데, 오토스케일러는 이를 트래픽 증가로 해석했습니다. 결과적으로 문제가 3배 악화되었습니다.

수치로 정리하면:

- pod당 커넥션 풀: 50개
- 정상 시 pod 수: 8개 (커넥션 400개)
- 장애 시 pod 수: 24개 (커넥션 1,200개)
- RDS max_connections: 1,000개
- 초과분: 200개 → 신규 커넥션 거부 → 헬스체크 실패 → pod 재시작 루프

## 권고 사항

즉시 적용이 필요한 것들입니다:

1. read-timeout을 3초로 축소하세요. 위 YAML을 참고하시기 바랍니다.
2. fallback 경로에 null 가드를 추가하고 테스트를 작성해야 합니다. 커버리지 0%인 경로가 프로덕션 최후의 방어선이라는 것은 용납되지 않아야 합니다.
3. HPA의 maxReplicas를 18로 제한하는 것이 요구됩니다. 18 x 50 = 900으로 max_connections 아래로 유지됩니다.
4. 커넥션 풀을 pod당 30개로 줄이는 것도 검토가 필요합니다. 현재 풀 사용률 피크가 60%라서 여유가 있습니다.

중기적으로는 PgBouncer 도입을 권합니다. pod 수와 DB 커넥션의 결합을 끊는 것이 근본 해결책입니다. 관련해서 스테이징에서 PoC를 진행할 예정입니다.

## 재발 방지

동일 장애의 재현 시나리오를 chaos engineering 테스트로 추가하는 것이 권장됩니다:

```bash
# PG사 레이턴시 주입 시뮬레이션
kubectl exec -it toxiproxy-0 -- /toxiproxy-cli toxic add \
  --toxicName pg-latency --type latency \
  --attribute latency=2000 --attribute jitter=500 payment-upstream
```

이 테스트가 월 1회 게임데이에서 수행되어야 합니다. 마지막으로, 이번 장애에서 on-call 엔지니어가 fallback NPE를 파악하는 데 14분이 소요되었는데, Sentry 알림에 소스 라인이 포함되지 않았던 것이 원인이었습니다. 알림 템플릿 개선도 검토해주세요.


## 2) gemma4:e2b — 0.0초

결제 서비스 장애의 근본 원인은 세 가지가 겹친 복합 장애입니다. 각각 단독으로는 문제가 되지 않았을 것이지만, 동시에 발생하면서 cascading failure가 트리거되었습니다.

## 타임라인

| 시각 | 이벤트 | 출처 |
|---|---|---|
| 02:13:05 | `payment-gateway` 레이턴시 p99가 180ms에서 2,400ms로 급증 | APM |
| 02:13:41 | `order-service` 스레드 풀 고갈 시작 (active 200/200) | 메트릭 |
| 02:14:02 | circuit breaker OPEN 전환, fallback 경로 활성화 | 앱 로그 |
| 02:14:33 | fallback 경로에서 NPE 다발, 에러율 34% 도달 | Sentry |
| 02:17:19 | 오토스케일러가 pod 8개에서 24개로 증설 | k8s 이벤트 |
| 02:19:05 | DB 커넥션 1,200개 도달, max_connections 초과 | RDS |
| 02:31:44 | 수동 롤백 완료, 에러율 정상화 | 배포 로그 |

## 근본 원인 1: 게이트웨이 레이턴시

인프라에서 02:13에 PG사 API의 응답 지연이 시작된 것을 발견했습니다. 이것은 외부 요인이라 통제가 불가능하지만, 문제는 타임아웃 설정이었습니다. `payment-gateway`의 HTTP 클라이언트 타임아웃이 30초로 설정되어 있었고, 이는 스레드가 최대 30초간 점유되는 것을 의미합니다. 업스트림 요청이 초당 40건인 상황에서 스레드 풀 200개가 고갈되는 데 걸리는 시간은 5초에 불과합니다.

설정은 다음과 같이 변경이 필요합니다:

```yaml
payment:
  http-client:
    connect-timeout: 500ms
    read-timeout: 3s        # 기존 30s
    retry:
      max-attempts: 2
      backoff: 200ms
```

## 근본 원인 2: fallback 경로의 NPE

circuit breaker가 열린 후 fallback 메서드가 호출되었는데, 이 경로는 작년 11월 이후 한 번도 실행된 적이 없었던 것으로 보입니다. 그 사이 `PaymentContext`에 `idempotencyKey` 필드가 추가되었고, fallback 경로에서는 이 필드의 초기화가 이루어지지 않았습니다.

```java
// PaymentFallbackHandler.java:47
public PaymentResult fallback(PaymentRequest req, Throwable t) {
    // req.getContext()가 null을 반환하는 경우가 처리되지 않았습니다
    String key = req.getContext().getIdempotencyKey(); // NPE 발생 지점
    return PaymentResult.pending(key);
}
```

테스트 커버리지를 확인해봤는데 fallback 경로의 커버리지는 0%였습니다. circuit breaker 관련 테스트가 존재하지 않는 것이 확인되었습니다.

## 근본 원인 3: 오토스케일러와 DB 커넥션의 상호작용

가장 흥미로운 부분입니다. 오토스케일러가 CPU 사용률을 기준으로 pod을 8개에서 24개로 늘렸는데, 각 pod은 커넥션 풀 50개가 있습니다. 24개 pod이면 최대 1,200개의 커넥션이 생성되는데, RDS의 max_connections는 1,000입니다. 즉 스케일 아웃이 오히려 DB를 죽이는 구조였던 것입니다. 레이턴시 문제로 CPU가 올라간 것인데, 오토스케일러는 이를 트래픽 증가로 해석했습니다. 결과적으로 문제가 3배 악화되었습니다.

수치로 정리하면:

- pod당 커넥션 풀: 50개
- 정상 시 pod 수: 8개 (커넥션 400개)
- 장애 시 pod 수: 24개 (커넥션 1,200개)
- RDS max_connections: 1,000개
- 초과분: 200개 → 신규 커넥션 거부 → 헬스체크 실패 → pod 재시작 루프

## 권고 사항

즉시 적용이 필요한 것들입니다:

1. read-timeout을 3초로 축소하세요. 위 YAML을 참고하시기 바랍니다.
2. fallback 경로에 null 가드를 추가하고 테스트를 작성해야 합니다. 커버리지 0%인 경로가 프로덕션 최후의 방어선이라는 것은 용납되지 않아야 합니다.
3. HPA의 maxReplicas를 18로 제한하는 것이 필요합니다. 18 x 50 = 900으로 max_connections 아래로 유지됩니다.
4. 커넥션 풀을 pod당 30개로 줄이는 것도 검토가 필요합니다. 현재 풀 사용률 피크가 60%라서 여유가 있습니다.

중기적으로는 PgBouncer 도입을 권합니다. pod 수와 DB 커넥션의 결합을 끊는 것이 근본 해결책입니다. 관련해서 스테이징에서 PoC를 진행할 예정입니다.

## 재발 방지

동일 장애의 재현 시나리오를 chaos engineering 테스트로 추가하는 것이 권장됩니다:

```bash
# PG사 레이턴시 주입 시뮬레이션
kubectl exec -it toxiproxy-0 -- /toxiproxy-cli toxic add \
  --toxicName pg-latency --type latency \
  --attribute latency=2000 --attribute jitter=500 payment-upstream
```

이 테스트가 월 1회 게임데이에서 수행되어야 합니다. 마지막으로, 이번 장애에서 on-call 엔지니어가 fallback NPE를 파악하는 데 14분이 소요되었는데, Sentry 알림에 소스 라인이 포함되지 않았던 것이 원인이었습니다. 알림 템플릿 개선도 검토해주세요.


---

**확인·조치 항목** (본문에서 그대로 모음)

- read-timeout을 3초로 축소하세요.
- 위 YAML을 참고하시기 바랍니다.
- fallback 경로에 null 가드를 추가하고 테스트를 작성해야 합니다.
- HPA의 maxReplicas를 18로 제한하는 것이 필요합니다.
- 커넥션 풀을 pod당 30개로 줄이는 것도 검토가 필요합니다.
- 중기적으로는 PgBouncer 도입을 권합니다.
- 알림 템플릿 개선도 검토해주세요.


## 3) exaone3.5:2.4b — 0.0초

결제 서비스 장애의 근본 원인은 세 가지가 겹친 복합 장애입니다. 각각 단독으로는 문제가 되지 않았을 것이지만, 동시에 발생하면서 cascading failure가 트리거되었습니다.

## 타임라인

| 시각 | 이벤트 | 출처 |
|---|---|---|
| 02:13:05 | `payment-gateway` 레이턴시 p99가 180ms에서 2,400ms로 급증 | APM |
| 02:13:41 | `order-service` 스레드 풀 고갈 시작 (active 200/200) | 메트릭 |
| 02:14:02 | circuit breaker OPEN 전환, fallback 경로 활성화 | 앱 로그 |
| 02:14:33 | fallback 경로에서 NPE 다발, 에러율 34% 도달 | Sentry |
| 02:17:19 | 오토스케일러가 pod 8개에서 24개로 증설 | k8s 이벤트 |
| 02:19:05 | DB 커넥션 1,200개 도달, max_connections 초과 | RDS |
| 02:31:44 | 수동 롤백 완료, 에러율 정상화 | 배포 로그 |

## 근본 원인 1: 게이트웨이 레이턴시

인프라에서 02:13에 PG사 API의 응답 지연이 시작된 것을 발견했습니다. 이것은 외부 요인이라 통제가 불가능하지만, 문제는 타임아웃 설정이었습니다. `payment-gateway`의 HTTP 클라이언트 타임아웃이 30초로 설정되어 있었고, 이는 스레드가 최대 30초간 점유되는 것을 의미합니다. 업스트림 요청이 초당 40건인 상황에서 스레드 풀 200개가 고갈되는 데 걸리는 시간은 5초에 불과합니다.

설정은 다음과 같이 변경이 필요합니다:

```yaml
payment:
  http-client:
    connect-timeout: 500ms
    read-timeout: 3s        # 기존 30s
    retry:
      max-attempts: 2
      backoff: 200ms
```

## 근본 원인 2: fallback 경로의 NPE

circuit breaker가 열린 후 fallback 메서드가 호출되었는데, 이 경로는 작년 11월 이후 한 번도 실행된 적이 없었던 것으로 보입니다. 그 사이 `PaymentContext`에 `idempotencyKey` 필드가 추가되었고, fallback 경로에서는 이 필드의 초기화가 이루어지지 않았습니다.

```java
// PaymentFallbackHandler.java:47
public PaymentResult fallback(PaymentRequest req, Throwable t) {
    // req.getContext()가 null을 반환하는 경우가 처리되지 않았습니다
    String key = req.getContext().getIdempotencyKey(); // NPE 발생 지점
    return PaymentResult.pending(key);
}
```

테스트 커버리지를 확인해봤는데 fallback 경로의 커버리지는 0%였습니다. circuit breaker 관련 테스트가 존재하지 않는 것이 확인되었습니다.

## 근본 원인 3: 오토스케일러와 DB 커넥션의 상호작용

가장 흥미로운 부분입니다. 오토스케일러가 CPU 사용률을 기준으로 pod을 8개에서 24개로 늘렸는데, 각 pod은 커넥션 풀 50개가 있습니다. 24개 pod이면 최대 1,200개의 커넥션이 생성되는데, RDS의 max_connections는 1,000입니다. 즉 스케일 아웃이 오히려 DB를 죽이는 구조였던 것입니다. 레이턴시 문제로 CPU가 올라간 것인데, 오토스케일러는 이를 트래픽 증가로 해석했습니다. 결과적으로 문제가 3배 악화되었습니다.

수치로 정리하면:

- pod당 커넥션 풀: 50개
- 정상 시 pod 수: 8개 (커넥션 400개)
- 장애 시 pod 수: 24개 (커넥션 1,200개)
- RDS max_connections: 1,000개
- 초과분: 200개 → 신규 커넥션 거부 → 헬스체크 실패 → pod 재시작 루프

## 권고 사항

즉시 적용이 필요한 것들입니다:

1. read-timeout을 3초로 축소하세요. 위 YAML을 참고하시기 바랍니다.
2. fallback 경로에 null 가드를 추가하고 테스트를 작성해야 합니다. 커버리지 0%인 경로가 프로덕션 최후의 방어선이라는 것은 용납되지 않아야 합니다.
3. HPA의 maxReplicas를 18로 제한하는 것이 필요합니다. 18 x 50 = 900으로 max_connections 아래로 유지됩니다.
4. 커넥션 풀을 pod당 30개로 줄이는 것도 검토가 필요합니다. 현재 풀 사용률 피크가 60%라서 여유가 있습니다.

중기적으로는 PgBouncer 도입을 권합니다. pod 수와 DB 커넥션의 결합을 끊는 것이 근본 해결책입니다. 관련해서 스테이징에서 PoC를 진행할 예정입니다.

## 재발 방지

동일 장애의 재현 시나리오를 chaos engineering 테스트로 추가하는 것이 권장됩니다:

```bash
# PG사 레이턴시 주입 시뮬레이션
kubectl exec -it toxiproxy-0 -- /toxiproxy-cli toxic add \
  --toxicName pg-latency --type latency \
  --attribute latency=2000 --attribute jitter=500 payment-upstream
```

이 테스트가 월 1회 게임데이에서 수행되어야 합니다. 마지막으로, 이번 장애에서 on-call 엔지니어가 fallback NPE를 파악하는 데 14분이 소요되었는데, Sentry 알림에 소스 라인이 포함되지 않았던 것이 원인이었습니다. 알림 템플릿 개선도 검토해주세요.


---

**확인·조치 항목** (본문에서 그대로 모음)

- read-timeout을 3초로 축소하세요.
- 위 YAML을 참고하시기 바랍니다.
- fallback 경로에 null 가드를 추가하고 테스트를 작성해야 합니다.
- HPA의 maxReplicas를 18로 제한하는 것이 필요합니다.
- 커넥션 풀을 pod당 30개로 줄이는 것도 검토가 필요합니다.
- 중기적으로는 PgBouncer 도입을 권합니다.
- 알림 템플릿 개선도 검토해주세요.
