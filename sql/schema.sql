-- ============================================================
-- CloudNETP v0.1.0 — MySQL 8.x 建表脚本
-- 字符集 utf8mb4 / InnoDB；时间列由应用层写入 UTC（naive）
-- 说明：也可以让应用启动时自动建表（SQLAlchemy create_all），
--       本脚本用于手工初始化或审阅结构。
-- ============================================================

CREATE DATABASE IF NOT EXISTS cloudnetp
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE cloudnetp;

-- ------------------------------------------------------------
-- 用户
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
  id            BIGINT       NOT NULL AUTO_INCREMENT,
  username      VARCHAR(32)  NOT NULL,
  password_hash VARCHAR(255) NOT NULL COMMENT 'scrypt$n$r$p$salt$hash',
  display_name  VARCHAR(32)  NULL COMMENT '昵称，空则回落到 username',
  bio           VARCHAR(200) NULL COMMENT '个人简介',
  avatar        VARCHAR(64)  NULL COMMENT '头像文件名，文件在 storage/avatars/',
  dav_ha1       VARCHAR(32)  NULL COMMENT 'WebDAV Digest 用的 MD5(user:realm:password)',
  role          TINYINT      NOT NULL DEFAULT 0 COMMENT '0 普通 1 管理员',
  quota_bytes   BIGINT       NOT NULL DEFAULT 10737418240 COMMENT '默认 10 GiB',
  used_bytes    BIGINT       NOT NULL DEFAULT 0 COMMENT '冗余累加，配额判断 O(1)',
  banned        TINYINT(1)   NOT NULL DEFAULT 0,
  token_version INT          NOT NULL DEFAULT 1 COMMENT '改密/踢下线时自增',
  created_at    DATETIME     NOT NULL,
  last_login_at DATETIME     NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_users_username (username),
  CONSTRAINT ck_users_role CHECK (role IN (0, 1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ------------------------------------------------------------
-- 用户偏好（与凭据分离，读多写少）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_settings (
  user_id       BIGINT     NOT NULL,
  theme_mode    TINYINT    NOT NULL DEFAULT 2 COMMENT '0 亮 1 暗 2 跟随系统',
  accent_color  VARCHAR(16) NOT NULL DEFAULT '#c084fc',
  wallpaper     VARCHAR(64) NOT NULL DEFAULT 'sakura',
  show_mascot   TINYINT(1) NOT NULL DEFAULT 1,
  show_petals   TINYINT(1) NOT NULL DEFAULT 1,
  trash_enabled TINYINT(1) NOT NULL DEFAULT 1,
  view_mode     VARCHAR(8) NOT NULL DEFAULT 'grid',
  nav_collapsed TINYINT(1) NOT NULL DEFAULT 0,
  share_show_name TINYINT(1) NOT NULL DEFAULT 0 COMMENT '分享页显示完整昵称，0 则只显示前两位',
  lyrics_style    JSON     NULL COMMENT '歌词播放器样式（fontSize/hotColor/showTrans）',
  PRIMARY KEY (user_id),
  CONSTRAINT fk_settings_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ------------------------------------------------------------
-- 每首歌（音频节点）的歌词来源偏好
--   mode: auto=优先级(内嵌→在线→无) / song=指定在线歌曲 /
--         custom=自定义歌词文本 / none=不显示
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audio_prefs (
  node_id       BIGINT      NOT NULL,
  mode          VARCHAR(8)  NOT NULL DEFAULT 'auto',
  song_id       BIGINT      NULL,
  song_title    VARCHAR(200) NOT NULL DEFAULT '',
  song_artist   VARCHAR(200) NOT NULL DEFAULT '',
  custom_lyrics MEDIUMTEXT  NULL,
  PRIMARY KEY (node_id),
  CONSTRAINT fk_audio_pref_node FOREIGN KEY (node_id) REFERENCES nodes (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ------------------------------------------------------------
-- 内容寻址的物理文件：同一 sha256 全站只存一份
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS blobs (
  sha256     CHAR(64)  NOT NULL,
  size       BIGINT    NOT NULL,
  ref_count  INT       NOT NULL DEFAULT 0,
  created_at DATETIME  NOT NULL,
  PRIMARY KEY (sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ------------------------------------------------------------
-- 逻辑节点树：文件夹与文件同表
--   · parent_id IS NULL = 用户根目录
--   · deleted_at IS NOT NULL = 在回收站
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS nodes (
  id                BIGINT       NOT NULL AUTO_INCREMENT,
  user_id           BIGINT       NOT NULL,
  parent_id         BIGINT       NULL,
  type              VARCHAR(6)   NOT NULL COMMENT 'folder / file',
  name              VARCHAR(255) NOT NULL,
  blob_sha256       CHAR(64)     NULL,
  size              BIGINT       NOT NULL DEFAULT 0,
  mime              VARCHAR(127) NULL,
  created_at        DATETIME     NOT NULL,
  updated_at        DATETIME     NOT NULL,
  deleted_at        DATETIME     NULL,
  restore_parent_id BIGINT       NULL COMMENT '删除时的原父，用于 O(1) 还原',
  PRIMARY KEY (id),
  KEY idx_node_parent (user_id, parent_id, deleted_at),
  KEY idx_node_search (user_id, deleted_at, name),
  KEY idx_node_trash  (user_id, deleted_at),
  KEY idx_node_blob   (blob_sha256),
  CONSTRAINT ck_nodes_type CHECK (type IN ('folder', 'file')),
  CONSTRAINT fk_node_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
  CONSTRAINT fk_node_blob FOREIGN KEY (blob_sha256) REFERENCES blobs (sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ------------------------------------------------------------
-- 分片上传会话
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS upload_sessions (
  id             CHAR(36)     NOT NULL COMMENT 'UUID4',
  user_id        BIGINT       NOT NULL,
  filename       VARCHAR(255) NOT NULL,
  size           BIGINT       NOT NULL,
  mime           VARCHAR(127) NULL,
  sha256         CHAR(64)     NOT NULL,
  target_node_id BIGINT       NULL,
  chunk_size     INT          NOT NULL,
  total_chunks   INT          NOT NULL,
  status         VARCHAR(10)  NOT NULL DEFAULT 'uploading',
  created_at     DATETIME     NOT NULL,
  updated_at     DATETIME     NOT NULL,
  PRIMARY KEY (id),
  KEY idx_sess_user (user_id, status),
  CONSTRAINT ck_sess_status CHECK (status IN ('uploading', 'completed', 'aborted')),
  CONSTRAINT fk_sess_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS upload_chunks (
  session_id  CHAR(36) NOT NULL,
  chunk_index INT      NOT NULL,
  size        INT      NOT NULL,
  sha256      CHAR(64) NOT NULL,
  PRIMARY KEY (session_id, chunk_index),
  CONSTRAINT fk_chunk_sess FOREIGN KEY (session_id) REFERENCES upload_sessions (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ------------------------------------------------------------
-- 分享链接
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shares (
  id             BIGINT      NOT NULL AUTO_INCREMENT,
  user_id        BIGINT      NOT NULL,
  node_id        BIGINT      NOT NULL,
  token          CHAR(43)    NOT NULL COMMENT 'secrets.token_urlsafe(32)',
  extract_code   VARCHAR(255) NULL COMMENT 'scrypt 哈希；NULL 表示免提取码',
  expires_at     DATETIME    NULL,
  max_downloads  INT         NULL,
  download_count INT         NOT NULL DEFAULT 0,
  created_at     DATETIME    NOT NULL,
  revoked        TINYINT(1)  NOT NULL DEFAULT 0,
  PRIMARY KEY (id),
  UNIQUE KEY uk_share_token (token),
  KEY idx_share_user (user_id, created_at),
  CONSTRAINT fk_share_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ------------------------------------------------------------
-- 分享举报（审核对象是分享链接，附文件哈希与首次被举报文件名）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS share_reports (
  id           BIGINT       NOT NULL AUTO_INCREMENT,
  share_id     BIGINT       NOT NULL,
  token        CHAR(43)     NOT NULL,
  blob_sha256  CHAR(64)     NULL COMMENT '单文件分享的文件哈希；文件夹分享为 NULL',
  first_name   VARCHAR(255) NOT NULL COMMENT '第一次被举报时的文件名',
  size         BIGINT       NOT NULL DEFAULT 0,
  is_folder    TINYINT(1)   NOT NULL DEFAULT 0,
  reason       VARCHAR(200) NOT NULL DEFAULT '',
  reporter_id  BIGINT       NULL COMMENT '登录用户举报时记录；匿名举报为 NULL',
  reporter_ip  VARCHAR(45)  NULL,
  status       VARCHAR(8)   NOT NULL DEFAULT 'pending' COMMENT 'pending/approved/rejected',
  handled_by   BIGINT       NULL,
  handled_at   DATETIME     NULL,
  created_at   DATETIME     NOT NULL,
  PRIMARY KEY (id),
  KEY idx_report_share (share_id),
  KEY idx_report_blob (blob_sha256),
  KEY idx_report_status (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ------------------------------------------------------------
-- 文件哈希黑名单：禁止分享/转存，不删除文件本体
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS blocked_blobs (
  sha256      CHAR(64)     NOT NULL,
  reason      VARCHAR(200) NOT NULL DEFAULT '',
  blocked_by  BIGINT       NULL,
  created_at  DATETIME     NOT NULL,
  PRIMARY KEY (sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ------------------------------------------------------------
-- 审计日志
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_logs (
  id         BIGINT      NOT NULL AUTO_INCREMENT,
  user_id    BIGINT      NULL,
  action     VARCHAR(32) NOT NULL,
  detail     TEXT        NULL,
  ip         VARCHAR(45) NULL,
  created_at DATETIME    NOT NULL,
  PRIMARY KEY (id),
  KEY idx_log_user (user_id, created_at),
  KEY idx_log_action (action, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ------------------------------------------------------------
-- 系统设置
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS system_settings (
  k VARCHAR(64) NOT NULL,
  v TEXT        NOT NULL,
  PRIMARY KEY (k)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT INTO system_settings (k, v) VALUES
  ('allow_register', '1'),
  ('site_name', 'CloudNETP'),
  ('site_announcement', ''),
  ('default_quota_bytes', '10737418240'),
  ('max_file_size_bytes', '4294967296'),
  ('chunk_size_bytes', '5242880'),
  ('trash_retention_days', '30')
ON DUPLICATE KEY UPDATE v = VALUES(v);
