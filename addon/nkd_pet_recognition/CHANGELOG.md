# Changelog

## 0.2.1
- Cat breeds: the `breed` and `breeds` attributes are now filled for cats too (API v3: 12 pedigree cat breeds). `breed` is empty when the API is not confident.

## 0.2.0
- Pet-recognition API v3: detects dogs and cats (`species` dog|cat|none, `species_score`, `animals`). Breeds: dogs only (cat breeds coming).
- New binary sensor "Cat detected" (unique_id `..._cat_detected`); "Dog detected" keeps its unique_id.
- Sensor "Animal": state dog, cat or none; attribute `animals` replaces `labels`.
- No animal is now HTTP 200 with species `none` (404 still handled as `none`).
- Event carries `species` and `animals`.

## 0.1.0
- Initial version. API client is a placeholder until the API spec is final.
