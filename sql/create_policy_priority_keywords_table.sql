-- Policy Priority Keywords Table
-- 질문군별 우선순위 키워드를 DB에서 관리하기 위한 테이블

CREATE TABLE IF NOT EXISTS gsnd_policy_priority (
    id INT AUTO_INCREMENT PRIMARY KEY,
    policy_tag VARCHAR(64) NOT NULL COMMENT 'implant | low_income | elderly_benefits',
    keyword VARCHAR(100) NOT NULL,
    priority_order INT NOT NULL DEFAULT 100 COMMENT '낮을수록 우선',
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_policy_tag_keyword (policy_tag, keyword),
    INDEX idx_policy_tag_active_order (policy_tag, is_active, priority_order)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 기본 시드 데이터
INSERT INTO gsnd_policy_priority (policy_tag, keyword, priority_order, is_active)
VALUES
    ('implant', '임플란트', 10, 1),
    ('implant', '치과', 20, 1),
    ('implant', '구강', 30, 1),
    ('implant', '의료급여', 40, 1),

    ('low_income', '생계급여', 10, 1),
    ('low_income', '의료급여', 20, 1),
    ('low_income', '저소득', 30, 1),
    ('low_income', '기초생활', 40, 1),

    ('elderly_benefits', '기초연금', 10, 1),
    ('elderly_benefits', '기초 연금', 20, 1),
    ('elderly_benefits', '노인맞춤돌봄', 30, 1),
    ('elderly_benefits', '노인 맞춤돌봄', 40, 1),
    ('elderly_benefits', '맞춤돌봄', 50, 1),
    ('elderly_benefits', '돌봄서비스', 60, 1)
ON DUPLICATE KEY UPDATE
    priority_order = VALUES(priority_order),
    is_active = VALUES(is_active),
    updated_at = CURRENT_TIMESTAMP;
