-- tpcc_schema.sql reduced to what egres accepts, for the egres microbenchmark
-- and for any backend that should run on the same tables (Dolt in the
-- egres comparison): the same eight tables, columns and order, with
--   DECIMAL(p, s) and FLOAT  -> DOUBLE PRECISION (egres has no numeric)
--   NOT NULL                 -> dropped (egres has no column constraints)
--   composite PRIMARY KEY    -> the most specific key column only, inline
--                               (egres: one single-column PRIMARY KEY)
-- The microbenchmark generates every key at random, so the single-column
-- keys are as unique as the composite ones were.

-- WAREHOUSE table
CREATE TABLE warehouse (
  w_id        SMALLINT PRIMARY KEY,
  w_name      VARCHAR(10),
  w_street_1  VARCHAR(20),
  w_street_2  VARCHAR(20),
  w_city      VARCHAR(20),
  w_state     CHAR(2),
  w_zip       CHAR(9),
  w_tax       DOUBLE PRECISION,
  w_ytd       DOUBLE PRECISION
);

-- DISTRICT table
CREATE TABLE district (
  d_id         SMALLINT PRIMARY KEY,
  d_w_id       SMALLINT,
  d_name       VARCHAR(10),
  d_street_1   VARCHAR(20),
  d_street_2   VARCHAR(20),
  d_city       VARCHAR(20),
  d_state      CHAR(2),
  d_zip        CHAR(9),
  d_tax        DOUBLE PRECISION,
  d_ytd        DOUBLE PRECISION,
  d_next_o_id  INT
);

-- CUSTOMER table
CREATE TABLE customer (
  c_id           INT PRIMARY KEY,
  c_d_id         SMALLINT,
  c_w_id         SMALLINT,
  c_first        VARCHAR(16),
  c_middle       CHAR(2),
  c_last         VARCHAR(16),
  c_street_1     VARCHAR(20),
  c_street_2     VARCHAR(20),
  c_city         VARCHAR(20),
  c_state        CHAR(2),
  c_zip          CHAR(9),
  c_phone        CHAR(16),
  c_since        BIGINT,
  c_credit       CHAR(2),
  c_credit_lim   DOUBLE PRECISION,
  c_discount     DOUBLE PRECISION,
  c_balance      DOUBLE PRECISION,
  c_ytd_payment  DOUBLE PRECISION,
  c_payment_cnt  SMALLINT,
  c_delivery_cnt SMALLINT,
  c_data         VARCHAR(500)
);

-- ITEM table
CREATE TABLE item (
  i_id     INT PRIMARY KEY,
  i_im_id  INT,
  i_name   VARCHAR(24),
  i_price  DOUBLE PRECISION,
  i_data   VARCHAR(50)
);

-- STOCK table
CREATE TABLE stock (
  s_i_id       INT PRIMARY KEY,
  s_w_id       SMALLINT,
  s_quantity   SMALLINT,
  s_dist_01    CHAR(24),
  s_dist_02    CHAR(24),
  s_dist_03    CHAR(24),
  s_dist_04    CHAR(24),
  s_dist_05    CHAR(24),
  s_dist_06    CHAR(24),
  s_dist_07    CHAR(24),
  s_dist_08    CHAR(24),
  s_dist_09    CHAR(24),
  s_dist_10    CHAR(24),
  s_ytd        DOUBLE PRECISION,
  s_order_cnt  SMALLINT,
  s_remote_cnt SMALLINT,
  s_data       VARCHAR(50)
);

-- ORDERS table
CREATE TABLE orders (
  o_id         INT PRIMARY KEY,
  o_d_id       SMALLINT,
  o_w_id       SMALLINT,
  o_c_id       INT,
  o_entry_d    BIGINT,
  o_carrier_id SMALLINT,
  o_ol_cnt     SMALLINT,
  o_all_local  SMALLINT
);

-- NEW_ORDER table
CREATE TABLE new_order (
  no_o_id  INT PRIMARY KEY,
  no_d_id  SMALLINT,
  no_w_id  SMALLINT
);

-- ORDER_LINE table
CREATE TABLE order_line (
  ol_o_id        INT PRIMARY KEY,
  ol_d_id        SMALLINT,
  ol_w_id        SMALLINT,
  ol_number      SMALLINT,
  ol_i_id        INT,
  ol_supply_w_id SMALLINT,
  ol_delivery_d  BIGINT,
  ol_quantity    SMALLINT,
  ol_amount      DOUBLE PRECISION,
  ol_dist_info   CHAR(24)
);
