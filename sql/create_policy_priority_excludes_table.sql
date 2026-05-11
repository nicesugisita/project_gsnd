-- Policy Priority Exclude Keywords Table
-- 정책 우선순위 태그를 강제로 무력화(null)시키는 변별 키워드를 관리.
-- LLM이 elderly_benefits/implant/low_income 등을 부여해도, 질문에 아래 키워드가
-- 부분일치하면 코드 단계에서 policy_priority_tag을 null로 강제한다.

CREATE TABLE IF NOT EXISTS gsnd_policy_priority_excludes (
    id INT AUTO_INCREMENT PRIMARY KEY,
    policy_tag VARCHAR(64) NOT NULL COMMENT 'implant | low_income | elderly_benefits',
    keyword VARCHAR(100) NOT NULL COMMENT '질문에 이 키워드가 들어가면 해당 태그를 null로 강제',
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    note VARCHAR(255) NULL COMMENT '운영 메모(예: 질환·신체부위·기능 등 분류)',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_excl_policy_tag_keyword (policy_tag, keyword),
    INDEX idx_excl_policy_tag_active (policy_tag, is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 초기 시드: elderly_benefits 변별 키워드 (질환/신체부위/기능)
INSERT INTO gsnd_policy_priority_excludes (policy_tag, keyword, is_active, note)
VALUES
    ('elderly_benefits', '치매',     1, '질환'),
    ('elderly_benefits', '우울',     1, '질환'),
    ('elderly_benefits', '우울증',   1, '질환'),
    ('elderly_benefits', '인지',     1, '기능'),
    ('elderly_benefits', '인지저하', 1, '기능'),
    ('elderly_benefits', '불면',     1, '질환'),
    ('elderly_benefits', '중풍',     1, '질환'),
    ('elderly_benefits', '뇌졸중',   1, '질환'),
    ('elderly_benefits', '파킨슨',   1, '질환'),
    ('elderly_benefits', '관절',     1, '신체부위'),
    ('elderly_benefits', '무릎',     1, '신체부위'),
    ('elderly_benefits', '허리',     1, '신체부위'),
    ('elderly_benefits', '어깨',     1, '신체부위'),
    ('elderly_benefits', '시력',     1, '기능'),
    ('elderly_benefits', '청력',     1, '기능'),
    ('elderly_benefits', '보행',     1, '기능'),
    ('elderly_benefits', '거동',     1, '기능')
ON DUPLICATE KEY UPDATE
    is_active   = VALUES(is_active),
    note        = VALUES(note),
    updated_at  = CURRENT_TIMESTAMP;
