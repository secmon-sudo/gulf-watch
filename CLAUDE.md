# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

---

# Projeye özel kurallar — gulf_watch

## 5. Kadans ve kota (bağlayıcı karar, 2026-09-07)

**ADS-B ingest günde bir kez çalışır (12:00 UTC). Kadans artırılmaz.** Bu bir tercih değil, ölçüm
sonucu:

- Analiz son *oturmuş* UTC gününe çapalı (`config.SETTLE_LAG_DAYS`). Fazladan çalışma aynı cevabı
  yeniden hesaplar, ama kota harcar.
- OpenSky kotası sabit saatte sıfırlanmıyor; harcandığı andan itibaren **kayan bir pencere**
  (2026-09-02'de ölçüldü: o gün hiçbir şey çalışmamışken 13:21'de `retry in 4.5h`). Her çalışma
  yarınki sıfırlama saatini kendi saatine iter.
- Ölçülen maliyet: bir havalimanı-günü ~30 kredi; on beş havalimanlık tam ingest sonrası
  `X-Rate-Limit-Remaining` 2170 (2026-08-24). Eski notlardaki "günde ~85 istek / bir geçiş ~52
  istek" rakamları bu hesapta hiç ölçülmedi — kullanılmaz.
- 15 dakikalık döngü bu katmanı kalıcı `outage`'a sokar.

**Baseline hasadı yalnızca elle tetiklenir.** `backfill-baseline.yml`'e `schedule:` bloğu
**eklenmez**. Eklendiği dönemde (2026-08-25..09-02) beş ingest'in üçü sıfır leg ile döndü ve
`observed_days` gerekli 5'in 2'sine düştü.

**Tek tüketici kuralı.** OpenSky kimlik bilgisini kullanan her iş ortak bir kota kaydından geçer:
son harcamanın zaman damgası + bilinen sıfırlama penceresi. Kayıt "harcanmış" diyorsa iş
**başlamadan** biter ve bunu çalışma detayı olarak bildirir. `concurrency: group: ingest` bunu
sağlamaz — o yalnızca eşzamanlılığı sıraya alır, kota harcamasını değil.

## 6. Kapsama kapısı: hangi sayıya bakılır

`observed_days` ve `coverage.verdict` sistemin sağlıklı olduğunu **kanıtlamaz**. 2026-09-06
ölçümünde ikisi de yeşildi (`5/5`, `ok(1.5)`, `ratios_published: true`) ama 24 taşıyıcının
**hiçbirinde** yayınlanabilir oran yoktu: `MIN_COMPARABLE_SHARE = 0.6` kapısını en iyi taşıyıcı
0.456 ile geçemiyordu. Baseline'lar sağlamdı (0.79-0.93); eksik olan bu haftanın rota görünürlüğü.

Sağlık iddiası şu göstergeye dayanır: **`carriers_with_ratio > 0` ve `comparable_share ≥ 0.6`.**

## 7. `legs=0` sessiz bir başarı değildir

Kotanın reddettiği bir çalışma ile trafiğin olmadığı bir gün aynı şey değildir. `recent_runs`
detayı bu ikisini ayrı sebeplerle raporlar: `quota_denied` ≠ `no_traffic`. Bu sözlük, birleşik
platform kontratındaki `withheld_reason` sözlüğüyle ortaktır.

> Gerekçelerin tamamı: `~/Desktop/github_repos/sec_swiss/AVSEC_SWISS_ARMY_KNIFE.md` §8 ve §11 Faz 0.
