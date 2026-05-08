CREATE TABLE IF NOT EXISTS gsnd_chat_history_anonymous (
    id INT AUTO_INCREMENT PRIMARY KEY,
    conv_id VARCHAR(255) NOT NULL,
    messages_json LONGTEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uniq_conv (conv_id),
    INDEX idx_updated_at (updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
