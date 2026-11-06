// Axum server (placeholder)
use std::env;
fn db_url() -> String { env::var("DATABASE_URL").unwrap() }
fn secret() -> Option<String> { env::var("SECRET_KEY").ok() }
const INSERT: &str = "INSERT INTO snags (id, status) VALUES ($1, $2)";
const HISTORY: &str = "SELECT * FROM snag_history WHERE snag_id = $1";
