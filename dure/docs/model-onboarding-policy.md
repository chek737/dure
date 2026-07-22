# 모델 반입·승인 정책

이 문서는 Dure 중앙 레지스트리에 모델·아티팩트·배치 profile을 넣기 전에 필요한 출처, 라이선스,
무결성, 보안, 운영 승인 기준을 정의한다. [모델 아티팩트 매니페스트와 배포 계약](artifact-distribution.md)은
파일·청크·cache의 기술 계약이고, 이 문서는 그 계약을 사용하기 전의 조직 책임을 다룬다.

현재 Fleet allowlist는 다음 네 Qwen2.5 Instruct AWQ 모델로 제한된다.

- `qwen2.5-7b-awq`
- `qwen2.5-14b-awq`
- `qwen2.5-32b-awq`
- `qwen2.5-72b-awq`

이 정책은 allowlist 밖 모델을 자동 허용하거나 runtime·stage 지원 범위를 넓히지 않는다. Dure가
모델 이름이나 repository만 보고 라이선스, 악성 파일, runtime 호환성을 자동 승인한다고 해석해서는
안 된다.

## 역할과 승인 상태

| 역할 | 책임 |
| --- | --- |
| 반입 요청자 | 원본 출처·revision·라이선스·용도·model card 제출 |
| 모델 관리자 | 정규 manifest, 양자화·아키텍처·runtime contract와 registry 입력 검토 |
| 보안·법무 승인자 | 배포 권한, remote code·공급망 위험, 접근 제한 승인 또는 거부 |
| 검증 운영자 | 정적 검사와 실제 GPU qualification evidence 수행·기록 |
| 중앙 운영자 | 승인된 immutable identity만 등록·승격하고 철회 시 영향 배포 통제 |

권장 상태는 아래와 같다. `DRAFT → QUALIFYING → VALIDATED → ACTIVE`는 구현된 profile 상태
계약이며, 앞부분의 검토 상태는 조직의 반입 관리 상태다.

```text
REQUESTED → SOURCE_REVIEW → LEGAL_REVIEW → DRAFT
                                      ↘ REJECTED
DRAFT → QUALIFYING → VALIDATED → ACTIVE
                  ↘ FAILED / REVOKED
```

## 반입 요청 필수 정보

요청자는 변경 가능한 웹 페이지 링크만이 아니라 보관 가능한 기록을 제출한다. token, 다운로드
cookie, private repository URL의 credential은 요청서나 Dure DB에 넣지 않는다.

| 항목 | 필요한 내용 |
| --- | --- |
| 원본 | publisher 또는 공식 배포자, repository URL, immutable revision/commit 또는 release ID, 획득 시각 |
| 권리 | model card, license 전문·버전, 상업적 사용·재배포·가중치 보관·파생 artifact 허용 여부 |
| identity | 모델 계열·정확한 양자화, tokenizer·config revision, bytes와 SHA-256 manifest digest |
| runtime | OCI image digest, vLLM·CUDA 계약, `TP`·`PP`, context·동시성 제한 |
| 위험 | remote code·Python 파일·adapter·LoRA·MoE·멀티모달 여부, 알려진 보안·라이선스 제한 |
| 운영 목적 | 허용 사용자·network zone·데이터 등급·예상 GPU/디스크·책임자 |

현재 `STAGE` 지원은 vLLM 0.9.0 V0, `Qwen2ForCausalLM`, AWQ, `TP=1`과 제한된 pipeline 계약으로
더 좁다. 자세한 범위는 [지원 매트릭스](support-matrix.md)와
[vLLM 단계 아티팩트 생성·검증·배포](stage-artifacts.md)를 기준으로 한다.

## 기술·보안 검토

1. 요청 revision의 정규 파일 목록·크기·SHA-256을 만들고 Dure artifact manifest digest를 계산한다.
   이후 등록·준비·배포는 이 immutable digest를 사용한다.
2. remote code는 허용하지 않는다. `trust_remote_code=false`에서 불가능한 입력, Python 모델 파일,
   `auto_map` 의존성, symlink·장치·특수 파일, 예상 밖 실행 파일은 거부한다.
3. 가중치와 tokenizer·config가 요청 architecture·quantization과 일치하는지 확인한다. 비슷한 모델명,
   이동 가능한 branch, tag만으로 동일성을 추정하지 않는다.
4. runtime은 OCI digest로 고정한다. mutable image tag, 사용자 Docker 인자, host NVIDIA driver 변경은
   모델 승인 경로가 아니다.
5. 비공개 모델 origin을 써야 하면 Agent root 전용 설정에만 credential을 둔다. 중앙 task·DB·결과에는
   raw URL, HTTP header, token을 넣지 않는다. 현재 artifact origin은 인증 header·token 전송을
   지원하지 않으므로 별도 승인 없이 우회하지 않는다.

## 레지스트리 등록과 승격

모델 관리자와 중앙 운영자는 source manifest, model revision, quantization, runtime image digest,
license 식별자와 allowlist ID를 함께 검토한 뒤에만 `DRAFT`로 등록한다. 등록 자체는 노드 다운로드,
image pull, container 실행, 기존 배포 교체를 만들지 않는다.

- `VALIDATED`는 정해진 모델·runtime·GPU·rank binding의 qualification evidence가 통과한 profile 또는
  stage variant다. 노드 설치나 서비스 가동 완료가 아니다.
- `ACTIVE`는 운영자가 선택 가능한 profile 상태다. Fleet recommendation과 deployment는 여전히
  현재 inventory, exact cache identity, image·network evidence, 명시적 prepare/apply를 요구한다.
- `PASSED`는 실제 GPU 실행 evidence의 결과다. `NOT_RUN` 또는 추정 VRAM은 `PASSED`가 아니다.

새 모델 계열, 양자화, PP, CUDA/vLLM runtime은 이 문서만 수정해 추가할 수 없다. 지원 코드, unit·GPU
검증, [릴리스 증적 기록](release-evidence/README.md), 지원 매트릭스 갱신을 함께 검토해야 한다.

## 철회·손상·라이선스 변경 대응

원본 publisher 철회, checksum 불일치, 악성 가능성, 라이선스 변경·위반 의심은 보안·법무 incident로
처리한다.

1. 영향 받은 revision·manifest digest·runtime·stage variant·profile·추천·배포를 식별하고 새
   recommendation·prepare·apply를 중지한다.
2. 실행 중 workload를 자동 중지·교체·rollback하지 않는다. 서비스 중단 권한을 확인한 뒤 exact Dure
   deployment label만 대상으로 명시적 중지 또는 검증된 generation rollback을 수행한다.
3. `STAGE` variant를 `REVOKED`로 만들면 향후 apply·start·restart·verify와 rollback target 시작은
   차단되지만, 기존 container나 node 파일을 자동 삭제하지 않는다. cache 격리는 preview와 명시적
   `artifact-cache quarantine --apply`를 거쳐야 한다.
4. 파일·cache를 삭제하기 전에는 [데이터 보존·격리·삭제 정책](data-retention.md)의 보존, legal hold,
   evidence 보존과 안전한 삭제 승인을 확인한다. 조사 중에는 evidence·hash·시간 기록을 삭제하지 않는다.
5. 대체 모델은 새 revision·manifest·qualification·승격·recommendation으로 처음부터 처리한다.
   기존 model ID나 package를 덮어쓰지 않는다.

## 현재 한계

Dure는 라이선스 판정, malware scanning, CVE 판정, 원본 publisher의 권리 확인을 자동화하지 않는다.
registry 등록이 모델 파일을 자동 다운로드하거나 모든 GPU 조합을 자동 검증하지도 않는다. 실제 모델
실행 범위는 코드와 evidence가 증명한 범위보다 넓게 표현하지 않는다.
