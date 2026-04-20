[Harness Rule Definition] (절대 제약 조건)
Zero Dependency (무의존성): 외부 라이브러리(redis, sqlalchemy 등) 사용을 엄격히 금지한다. 파이썬 표준 라이브러리(sqlite3, hashlib)만으로 O(1) 수준의 검색 속도를 보장해야 한다.

Deterministic Hashing (결정론적 키 생성): 캐시 키는 엔드포인트 URL과 파라미터의 조합을 SHA-256으로 해싱하여 생성한다. 단, 보안을 위해 API_KEY는 해싱 및 DB 저장 대상에서 영구적으로 제외한다.

Strict TTL (수명 주기 통제): 재무 데이터는 실시간성이 필요 없다. 한 번 긁어온 DART 데이터는 7일(604,800초) 동안 디스크에 박제하며, 만료된 데이터는 쿼리 시점에 가비지 컬렉터(GC)가 즉각 파괴한다.

Concurrency Safe (동시성 방어): Streamlit의 멀티스레딩 환경에서 DB 락(Lock)이 걸리지 않도록 PRAGMA journal_mode=WAL;을 강제한다.