-- Message Feedback Table
-- Stores copy and like/dislike feedback per assistant message

CREATE TABLE IF NOT EXISTS gsnd_message_feedback (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id VARCHAR(255) NULL,
    conv_id VARCHAR(255) NOT NULL,
    message_index INT NOT NULL,
    reaction VARCHAR(16) NULL,
    copied_count INT NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uniq_feedback (conv_id, message_index),
    INDEX idx_user_id (user_id),
    INDEX idx_conv_id (conv_id),
    INDEX idx_updated_at (updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
