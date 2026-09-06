--------------------------------------------------------------------------------
--  FILE:    SA_06_de_user.sql
--  RUN AS:  ADMIN
--  PURPOSE: Create the Data Engineer login and grant it DE-level provisioning
--           rights over the agent schema.
--
--  The DE and the DS are deliberately NOT the same user. The DE provisions:
--  it creates credentials, reaches Object Storage, comments tables, and reads
--  across schemas. The DS only builds and runs agents inside one schema.
--  Pre-flight option 1 -> 2 checks this user; option 1 -> 1 checks the DS.
--
--  Run SA_06_ds_user.sql afterwards for the narrower DS login.
--------------------------------------------------------------------------------

SET VERIFY OFF
SET SERVEROUTPUT ON
SET LINESIZE 130
SET PAGESIZE 200
SET DEFINE '^'

DEFINE de_user      = DE_USER
DEFINE agent_schema = ACME_CORP

PROMPT
PROMPT ============================================================
PROMPT  Creating DE login ^de_user. over schema ^agent_schema.
PROMPT ============================================================
PROMPT
PROMPT  Password rules (Autonomous Database):
PROMPT    - 12 to 30 characters
PROMPT    - at least one uppercase, one lowercase, one digit
PROMPT    - no double quote ("), and cannot contain the username
PROMPT

ACCEPT de_pw CHAR PROMPT 'Password for ^de_user.: ' HIDE

-- ============================================================================
-- SECTION 1: CREATE USER  (idempotent -- safe to re-run)
-- ============================================================================
DECLARE
    v_exists NUMBER;
    v_pwd    VARCHAR2(4000) := '^de_pw';
BEGIN
    IF v_pwd IS NULL OR LENGTH(v_pwd) = 0 THEN
        RAISE_APPLICATION_ERROR(-20001, 'No password entered.');
    END IF;

    SELECT COUNT(*) INTO v_exists
    FROM   dba_users
    WHERE  username = '^de_user.';

    IF v_exists > 0 THEN
        DBMS_OUTPUT.PUT_LINE('^de_user. already exists -- skipping CREATE USER.');
        DBMS_OUTPUT.PUT_LINE('To reset the password instead, use:');
        DBMS_OUTPUT.PUT_LINE('  ALTER USER ^de_user. IDENTIFIED BY "<new-password>";');
    ELSE
        EXECUTE IMMEDIATE
            'CREATE USER ^de_user. IDENTIFIED BY "' || v_pwd || '" '
         || 'DEFAULT TABLESPACE DATA '
         || 'TEMPORARY TABLESPACE TEMP '
         || 'QUOTA UNLIMITED ON DATA';
        DBMS_OUTPUT.PUT_LINE('^de_user. created.');
    END IF;
END;
/

-- ============================================================================
-- SECTION 2: SYSTEM PRIVILEGES
-- ============================================================================
-- These three are what pre-flight's DE_SYS_PRIVS list checks for.
--
-- They are granted DIRECTLY, not through a role. Pre-flight reads
-- SESSION_PRIVS from a direct (non-proxied) login, and a privilege held
-- only through a non-default role will not appear there. This is the same
-- default-role trap that makes NL2SQL miss role-granted table access.

GRANT CREATE SESSION         TO ^de_user.;
GRANT SELECT ANY TABLE       TO ^de_user.;
GRANT SELECT ANY DICTIONARY  TO ^de_user.;
GRANT COMMENT ANY TABLE      TO ^de_user.;

-- ============================================================================
-- SECTION 3: PACKAGE EXECUTE  (pre-flight DE_PACKAGES)
-- ============================================================================
-- DBMS_CLOUD and DBMS_CLOUD_ADMIN are the DE/DS dividing line. The DE needs
-- them to create credentials and reach Object Storage; the DS does not get
-- them at all.

GRANT EXECUTE ON DBMS_CLOUD_AI       TO ^de_user.;
GRANT EXECUTE ON DBMS_CLOUD_AI_AGENT TO ^de_user.;
GRANT EXECUTE ON DBMS_CLOUD          TO ^de_user.;
GRANT EXECUTE ON DBMS_CLOUD_ADMIN    TO ^de_user.;

-- ============================================================================
-- SECTION 4: PROXY CONNECT
-- ============================================================================
-- Lets the DE connect as DE_USER[ACME_CORP] and create objects that the
-- agent schema owns, without ever knowing the schema password.
--
-- DEFAULT ROLE ALL matters: without it the proxied session starts with no
-- roles enabled, and privileges the schema holds through a role go missing
-- in a way that is very hard to read from the error.

ALTER USER ^agent_schema. GRANT CONNECT THROUGH ^de_user.;
ALTER USER ^agent_schema. DEFAULT ROLE ALL;

-- ============================================================================
-- SECTION 5: VERIFY
-- ============================================================================
PROMPT
PROMPT --- User
SELECT username, account_status, default_tablespace
FROM   dba_users
WHERE  username = '^de_user.';

PROMPT
PROMPT --- System privileges (expect: COMMENT ANY TABLE, CREATE SESSION,
PROMPT ---                            SELECT ANY DICTIONARY, SELECT ANY TABLE)
SELECT privilege
FROM   dba_sys_privs
WHERE  grantee = '^de_user.'
ORDER  BY privilege;

PROMPT
PROMPT --- Package EXECUTE (expect 4 rows)
-- DBMS_CLOUD is a PUBLIC SYNONYM, not a package. The grant lands on the
-- versioned package it points at -- DBMS_CLOUD$PDBCS_<version> owned by
-- C##CLOUD$SERVICE -- so looking for the literal name 'DBMS_CLOUD' in
-- DBA_TAB_PRIVS finds nothing even though the grant succeeded. Match the
-- versioned form. (Pre-flight avoids this entirely by CALLING each package
-- rather than reading the privilege views.)
SELECT table_name AS package_name
FROM   dba_tab_privs
WHERE  grantee   = '^de_user.'
AND    privilege = 'EXECUTE'
AND    (table_name IN ('DBMS_CLOUD_AI','DBMS_CLOUD_AI_AGENT','DBMS_CLOUD_ADMIN')
        OR table_name LIKE 'DBMS_CLOUD$%')
ORDER  BY table_name;

PROMPT
PROMPT --- Proxy grant (expect ^agent_schema. <- ^de_user.)
SELECT client, proxy
FROM   dba_proxies
WHERE  proxy = '^de_user.';

PROMPT
PROMPT ============================================================
PROMPT  Done.
PROMPT
PROMPT  Set in agent_builder_config.ini:   [de] de_schema = ^de_user.
PROMPT  Then:  export OCI_DB_PASSWORD_^de_user.='<the password>'
PROMPT  Then:  python agent_builder.py  ->  1  ->  2
PROMPT ============================================================
PROMPT

UNDEFINE de_pw
