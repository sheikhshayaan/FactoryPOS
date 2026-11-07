-- Password recovery without e-mail: each user may keep one security question. The answer is stored only as an Argon2id hash.
ALTER TABLE users ADD COLUMN security_question TEXT;
ALTER TABLE users ADD COLUMN security_answer_hash TEXT;
ALTER TABLE users ADD COLUMN reset_failed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN reset_locked_until TEXT;
