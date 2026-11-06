CREATE TABLE snags (id uuid PRIMARY KEY, status text NOT NULL);
CREATE TABLE snag_history (id bigserial PRIMARY KEY, snag_id uuid REFERENCES snags(id));
