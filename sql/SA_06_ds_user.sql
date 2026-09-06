--------------------------------------------------------------------------------
--  FILE:    SA_06_ds_user.sql
--  RUN AS:  ADMIN  (or a DE with CREATE USER)
--  PURPOSE: Create the Data Scientist login -- the narrow one.
--
--  The DS builds and runs agents inside one schema. It gets the two Select AI
--  packages and nothing else. Specifically it does NOT get DBMS_CLOUD,
--  DBMS_CLOUD_ADMIN, or any of the ANY-privileges: those are the DE's, and
--  the whole point of splitting the two logins is that a DS cannot mint
--  credentials or read across the database.
--
--  Pre-flight option 1 -> 1 checks this user.
--------------------------------------------------------------------------------

SET VERIFY OFF
SET SERVEROUTPUT ON
SET LINESIZE 130
SET PAGESIZE 200
SET DEFINE '^'

DEFINE ds_user      = DS_USER
DEFINE agent_schema = ACME_CORP

PROMPT
PROMPT ============================================================
PROMPT  Creating DS login ^ds_user. over schema ^agent_schema.
PROMPT ============================================================
PROMPT
PROMPT  Password rules (Autonomous Database):
PROMPT    - 12 to 30 characters
PROMPT    - at least one uppercase, one lowercase, one digit
PROMPT    - no double quote ("), and cannot contain the username
PROMPT

ACCEPT ds_pw CHAR PROMPT 'Password for ^ds_user.: ' HIDE

-- ============================================================================
-- SECTION 1: CREATE USER  (idempotent -- safe to re-run)
-- ============================================================================
DECLARE
    v_exists NUMBER;
    v_pwd    VARCHAR2(4000) := '^ds_pw';
BEGIN
    IF v_pwd IS NULL OR LENGTH(v_pwd) = 0 THEN
        RAISE_APPLICATION_ERROR(-20001, 'No password entered.');
    END IF;

    SELECT COUNT(*) INTO v_exists
    FROM   dba_users
    WHERE  username = '^ds_user.';

    IF v_exists > 0 THEN
        DBMS_OUTPUT.PUT_LINE('^ds_user. already exists -- skipping CREATE USER.');
        DBMS_OUTPUT.PUT_LINE('To reset the password instead, use:');
        DBMS_OUTPUT.PUT_LINE('  ALTER USER ^ds_user. IDENTIFIED BY "<new-password>";');
    ELSE
        EXECUTE IMMEDIATE
            'CREATE USER ^ds_user. IDENTIFIED BY "' || v_pwd || '" '
         || 'DEFAULT TABLESPACE DATA '
         || 'TEMPORARY TABLESPACE TEMP '
         || 'QUOTA UNLIMITED ON DATA';
        DBMS_OUTPUT.PUT_LINE('^ds_user. created.');
    END IF;
END;
/

-- ============================================================================
-- SECTION 2: PRIVILEGES  (deliberately short)
-- ============================================================================
GRANT CREATE SESSION TO ^ds_user.;

GRANT EXECUTE ON DBMS_CLOUD_AI       TO ^ds_user.;
GRANT EXECUTE ON DBMS_CLOUD_AI_AGENT TO ^ds_user.;

-- Not granted, on purpose -- leave these commented out:
--   GRANT EXECUTE ON DBMS_CLOUD       TO ^ds_user.;
--   GRANT EXECUTE ON DBMS_CLOUD_ADMIN TO ^ds_user.;
--   GRANT SELECT ANY TABLE            TO ^ds_user.;
--   GRANT SELECT ANY DICTIONARY       TO ^ds_user.;
--   GRANT COMMENT ANY TABLE           TO ^ds_user.;

-- ============================================================================
-- SECTION 3: PROXY CONNECT
-- ============================================================================
ALTER USER ^agent_schema. GRANT CONNECT THROUGH ^ds_user.;
ALTER USER ^agent_schema. DEFAULT ROLE ALL;

-- ============================================================================
-- SECTION 4: VERIFY
-- ============================================================================
PROMPT
PROMPT --- User
SELECT username, account_status, default_tablespace
FROM   dba_users
WHERE  username = '^ds_user.';

PROMPT
PROMPT --- System privileges (expect CREATE SESSION only)
SELECT privilege
FROM   dba_sys_privs
WHERE  grantee = '^ds_user.'
ORDER  BY privilege;

PROMPT
PROMPT --- Package EXECUTE (expect 2 rows: the two Select AI packages)
-- A DBMS_CLOUD$PDBCS_<version> row here means someone granted DBMS_CLOUD to
-- the DS user -- that is the DE's package, and it should not appear.
SELECT table_name AS package_name
FROM   dba_tab_privs
WHERE  grantee   = '^ds_user.'
AND    privilege = 'EXECUTE'
AND    table_name LIKE 'DBMS_CLOUD%'
ORDER  BY table_name;

PROMPT
PROMPT --- Proxy grant
SELECT client, proxy
FROM   dba_proxies
WHERE  proxy = '^ds_user.';

PROMPT
PROMPT ============================================================
PROMPT  Done.
PROMPT
PROMPT  Set in agent_builder_config.ini:   [database] db_user = ^ds_user.
PROMPT  Then:  export OCI_DB_PASSWORD_^ds_user.='<the password>'
PROMPT  Then:  python agent_builder.py  ->  1  ->  1
PROMPT ============================================================
PROMPT

UNDEFINE ds_pw
