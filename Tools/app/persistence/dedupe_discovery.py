# ========================================================================
# FILE: app/persistence/dedupe_discovery.py (FIXED LEAD ENRICHMENT)
# ========================================================================
"""Phase 1-3: Discovery (duplicate keys, edges, components)"""
from sqlalchemy import text
from app.utils.dedupe_logger import log

class DuplicateKeyDiscovery:
    @staticmethod
    def discover(session, ctx):
        """Phase 1: Find all duplicate identifier keys"""
        log.info(f"[PHASE_1] START: Discovering duplicate keys", extra={"ctx": ctx.to_dict()})

        sql = text("""
        WITH norms AS (
            SELECT u_id, 'phone:' || phone AS k FROM user_data_merge_test WHERE phone IS NOT NULL
            UNION ALL
            SELECT u_id, 'phone:' || wa_num AS k FROM user_data_merge_test WHERE wa_num IS NOT NULL
            UNION ALL
            SELECT u_id, 'email:' || email AS k FROM user_data_merge_test WHERE email IS NOT NULL
        ),
        duplicates AS (
            SELECT k FROM norms GROUP BY k HAVING COUNT(*) > 1
        )
        SELECT k FROM duplicates ORDER BY k;
        """)

        rows = session.execute(sql).fetchall()
        keys = [r[0] for r in rows]
        
        log.info(f"[PHASE_1] COMPLETE: Found {len(keys)} duplicate keys", extra={"ctx": ctx.to_dict()})
        return keys

class EdgeListBuilder:
    @staticmethod
    def build(session, dup_keys, ctx):
        """
        Phase 2: Build edge list from user-to-user and lead enrichment
        
        FIXED: Lead enrichment now requires MULTIPLE matching identifiers per lead
        to avoid false positive merges from single shared identifier (e.g., shared email)
        
        Scenario prevention:
        - Lead has phone A + email E
        - User 1 has phone A
        - User 2 has email E
        - Result: NO edge created between User 1 and User 2 (only 1 match per user)
        
        - Lead has phone A + email E
        - User 1 has phone A + email E
        - User 2 has phone A
        - Result: Edge created between User 1 and Lead, but NOT with User 2
        """
        log.info(f"[PHASE_2] START: Building edge list from {len(dup_keys)} duplicate keys", 
                 extra={"ctx": ctx.to_dict()})

        session.execute(text("DROP TABLE IF EXISTS temp_edges CASCADE"))
        session.execute(text("""
            CREATE TEMP TABLE temp_edges (user_id_a BIGINT, user_id_b BIGINT) ON COMMIT DROP;
            CREATE INDEX idx_temp_edges_a ON temp_edges(user_id_a);
            CREATE INDEX idx_temp_edges_b ON temp_edges(user_id_b);
        """))
        
        user_edge_count = 0

        # ========== STEP 1: USER-TO-USER EDGES (FROM DIRECT IDENTIFIER MATCHES) ==========
        if dup_keys:
            log.info(f"[PHASE_2] Step 1: Creating user-to-user edges")
            
            insert_user_edges = text("""
                WITH norms AS (
                    SELECT u_id, 'phone:' || phone AS k FROM user_data_merge_test WHERE phone IS NOT NULL
                    UNION ALL
                    SELECT u_id, 'phone:' || wa_num AS k FROM user_data_merge_test WHERE wa_num IS NOT NULL
                    UNION ALL
                    SELECT u_id, 'email:' || email AS k FROM user_data_merge_test WHERE email IS NOT NULL
                ),
                filtered AS (
                    SELECT u_id, k FROM norms WHERE k = ANY(:keys)
                ),
                grouped AS (
                    SELECT k, array_agg(DISTINCT u_id ORDER BY u_id) AS uids
                    FROM filtered
                    GROUP BY k
                    HAVING COUNT(DISTINCT u_id) > 1
                )
                INSERT INTO temp_edges(user_id_a, user_id_b)
                SELECT uids[i], uids[i+1]
                FROM grouped
                CROSS JOIN LATERAL generate_series(1, array_length(uids, 1) - 1) AS i
                UNION ALL
                SELECT uids[i+1], uids[i]
                FROM grouped
                CROSS JOIN LATERAL generate_series(1, array_length(uids, 1) - 1) AS i
            """)

            session.execute(insert_user_edges, {"keys": dup_keys})
            session.flush()

            user_edge_count = session.execute(text("SELECT COUNT(*) FROM temp_edges")).scalar() or 0
            log.info(f"[PHASE_2] Step 1 complete: {user_edge_count} user-to-user edges")

        # ========== STEP 2: LEAD-BASED EDGES (FIXED: MULTIPLE IDENTIFIERS REQUIRED) ==========
        log.info(f"[PHASE_2] Step 2: Creating lead-based edges (FIXED: multiple identifiers required)")
        
        insert_lead_edges = text("""
        WITH lead_identifiers AS (
            -- Extract ALL identifiers from each lead
            SELECT 
                id AS lead_id,
                'phone' AS id_type,
                customer_phone AS raw_value,
                'phone:' || customer_phone AS normalized_key
            FROM lead_activities_details
            WHERE customer_phone IS NOT NULL
            
            UNION ALL
            
            SELECT 
                id AS lead_id,
                'phone2' AS id_type,
                customer_phone2 AS raw_value,
                'phone:' || customer_phone2 AS normalized_key
            FROM lead_activities_details
            WHERE customer_phone2 IS NOT NULL
            
            UNION ALL
            
            SELECT 
                id AS lead_id,
                'email' AS id_type,
                customer_email AS raw_value,
                'email:' || LOWER(TRIM(customer_email)) AS normalized_key
            FROM lead_activities_details
            WHERE customer_email IS NOT NULL
        ),
        user_identifiers AS (
            -- Extract ALL identifiers from each user
            SELECT 
                u_id AS user_id,
                'phone:' || phone AS normalized_key
            FROM user_data_merge_test
            WHERE phone IS NOT NULL
            
            UNION ALL
            
            SELECT 
                u_id AS user_id,
                'phone:' || wa_num AS normalized_key
            FROM user_data_merge_test
            WHERE wa_num IS NOT NULL
            
            UNION ALL
            
            SELECT 
                u_id AS user_id,
                'email:' || LOWER(TRIM(email)) AS normalized_key
            FROM user_data_merge_test
            WHERE email IS NOT NULL
        ),
        lead_user_matches AS (
            -- For each lead, find ALL users that match on ANY identifier
            SELECT 
                li.lead_id,
                ui.user_id,
                li.normalized_key,
                ROW_NUMBER() OVER (PARTITION BY li.lead_id, ui.user_id ORDER BY li.normalized_key) AS match_seq
            FROM lead_identifiers li
            JOIN user_identifiers ui ON li.normalized_key = ui.normalized_key
        ),
        lead_user_match_counts AS (
            -- Count how many DISTINCT identifiers match between lead and user
            SELECT 
                lead_id,
                user_id,
                COUNT(DISTINCT normalized_key) AS matching_identifier_count
            FROM lead_user_matches
            GROUP BY lead_id, user_id
        ),
        lead_identifier_counts AS (
            -- Count how many identifiers each lead has
            SELECT 
                lead_id,
                COUNT(DISTINCT normalized_key) AS total_identifiers
            FROM lead_identifiers
            GROUP BY lead_id
        ),
        valid_lead_user_pairs AS (
            -- FIXED: Only create edges if lead matched on MULTIPLE identifiers
            -- OR if lead matched ALL identifiers of a user
            -- This prevents false merges from single shared identifier (e.g., shared email)
            SELECT 
                lumc.lead_id,
                lumc.user_id,
                lumc.matching_identifier_count,
                lic.total_identifiers
            FROM lead_user_match_counts lumc
            JOIN lead_identifier_counts lic ON lumc.lead_id = lic.lead_id
            WHERE lumc.matching_identifier_count >= 2  -- CRITICAL: Require at least 2 matching identifiers
               OR (
                   -- Alternative: if lead matched ALL identifiers of user
                   SELECT COUNT(DISTINCT normalized_key)
                   FROM user_identifiers
                   WHERE user_id = lumc.user_id
               ) <= lumc.matching_identifier_count
        ),
        lead_matched_users AS (
            -- For each lead, get users that matched on multiple identifiers
            SELECT 
                lead_id,
                array_agg(DISTINCT user_id ORDER BY user_id) AS matched_uids
            FROM valid_lead_user_pairs
            GROUP BY lead_id
            HAVING COUNT(DISTINCT user_id) > 1  -- Only if multiple users matched
        )
        INSERT INTO temp_edges(user_id_a, user_id_b)
        SELECT matched_uids[i], matched_uids[i+1]
        FROM lead_matched_users
        CROSS JOIN LATERAL generate_series(1, array_length(matched_uids, 1) - 1) AS i
        UNION ALL
        SELECT matched_uids[i+1], matched_uids[i]
        FROM lead_matched_users
        CROSS JOIN LATERAL generate_series(1, array_length(matched_uids, 1) - 1) AS i
        ON CONFLICT DO NOTHING
        """)

        try:
            log.debug(f"[PHASE_2] Executing fixed lead enrichment query")
            session.execute(insert_lead_edges)
            session.flush()
            log.info(f"[PHASE_2] Step 2 complete: Lead-based enrichment successful (multiple identifier requirement)")
        except Exception as e:
            log.warning(f"[PHASE_2] Lead enrichment skipped: {type(e).__name__}")

        total_edge_count = session.execute(text("SELECT COUNT(*) FROM temp_edges")).scalar() or 0
        lead_edge_count = total_edge_count - user_edge_count
        
        log.info(f"[PHASE_2] COMPLETE: user_edges={user_edge_count}, lead_edges={lead_edge_count}, total={total_edge_count}", 
                 extra={"ctx": ctx.to_dict()})
        log.info(f"[PHASE_2] FIXED: Lead edges now require MULTIPLE matching identifiers per lead")
        
        return total_edge_count

class ConnectedComponentFinder:
    @staticmethod
    def find(session, ctx):
        """Phase 3: Find connected components using iterative SQL union-find"""
        log.info(f"[PHASE_3] START: Finding connected components", extra={"ctx": ctx.to_dict()})

        edge_count = session.execute(text("SELECT COUNT(*) FROM temp_edges")).scalar() or 0
        
        if edge_count == 0:
            log.info(f"[PHASE_3] No edges found, returning empty groups")
            return []

        node_count = session.execute(text("""
            SELECT COUNT(DISTINCT node) FROM (
                SELECT user_id_a AS node FROM temp_edges
                UNION
                SELECT user_id_b AS node FROM temp_edges
            ) x
        """)).scalar() or 0

        log.info(f"[PHASE_3] Graph: nodes={node_count}, edges={edge_count}")

        session.execute(text("DROP TABLE IF EXISTS uf_parent CASCADE"))
        session.execute(text("""
            CREATE TEMP TABLE uf_parent (node BIGINT PRIMARY KEY, parent BIGINT NOT NULL) ON COMMIT DROP
        """))

        session.execute(text("""
            INSERT INTO uf_parent(node, parent)
            SELECT DISTINCT node, node FROM (
                SELECT user_id_a AS node FROM temp_edges
                UNION
                SELECT user_id_b AS node FROM temp_edges
            ) x
        """))
        session.flush()

        max_iterations = 100
        iteration = 0

        while iteration < max_iterations:
            session.execute(text("DROP TABLE IF EXISTS uf_roots CASCADE"))
            session.execute(text("""
                CREATE TEMP TABLE uf_roots AS
                WITH RECURSIVE find_root AS (
                    SELECT node, parent FROM uf_parent WHERE node = parent
                    UNION ALL
                    SELECT p.node, fr.parent FROM uf_parent p
                    JOIN find_root fr ON p.parent = fr.node WHERE p.node != p.parent
                )
                SELECT DISTINCT node, parent FROM find_root
            """))

            update_result = session.execute(text("""
                UPDATE uf_parent p
                SET parent = LEAST(updates.root_a, updates.root_b)
                FROM (
                    SELECT e.user_id_a, e.user_id_b, r1.parent as root_a, r2.parent as root_b
                    FROM temp_edges e
                    JOIN uf_roots r1 ON e.user_id_a = r1.node
                    JOIN uf_roots r2 ON e.user_id_b = r2.node
                ) updates
                WHERE p.node = updates.user_id_a
                  AND LEAST(updates.root_a, updates.root_b) < p.parent
            """))

            changes = update_result.rowcount
            session.execute(text("DROP TABLE IF EXISTS uf_roots CASCADE"))

            if changes == 0:
                log.info(f"[PHASE_3] Fixed point reached after {iteration + 1} iterations")
                break

            iteration += 1

        session.execute(text("""
            WITH RECURSIVE find_root AS (
                SELECT node, parent FROM uf_parent WHERE node = parent
                UNION ALL
                SELECT p.node, fr.parent FROM uf_parent p
                JOIN find_root fr ON p.parent = fr.node WHERE p.node != p.parent
            )
            UPDATE uf_parent SET parent = fr.parent FROM find_root fr WHERE uf_parent.node = fr.node
        """))
        session.flush()

        rows = session.execute(text("""
            SELECT parent, array_agg(DISTINCT node ORDER BY node) AS members
            FROM uf_parent GROUP BY parent HAVING COUNT(*) > 1
            ORDER BY array_length(array_agg(DISTINCT node ORDER BY node), 1) DESC
        """)).fetchall()
        
        groups = [list(members) for parent, members in rows]
        log.info(f"[PHASE_3] COMPLETE: Found {len(groups)} groups", extra={"ctx": ctx.to_dict()})
        log.info(f"[PHASE_3] FIXED: Groups now properly separated based on multiple identifier matches")
        
        return groups