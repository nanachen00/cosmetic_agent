import os
from typing import Optional

from neo4j import GraphDatabase


class ProfileRepository:
    def __init__(self):
        # Legacy compatibility only. Personal data uses PostgreSQL when
        # AUTH_STORAGE_BACKEND=postgres; Neo4j credentials must never be
        # embedded in source control.
        self.neo4j_uri = os.environ["NEO4J_URI"]
        self.neo4j_user = os.environ["NEO4J_USER"]
        self.neo4j_password = os.environ["NEO4J_PASSWORD"]

        self.neo4j_driver = GraphDatabase.driver(
            self.neo4j_uri,
            auth=(
                self.neo4j_user,
                self.neo4j_password,
            ),
            keep_alive=True,
        )

    def verify_connectivity(self) -> None:
        """確認 Neo4j 是否可以正常連線。"""
        self.neo4j_driver.verify_connectivity()

    def create_constraints(self) -> None:
        """建立 User.user_id 唯一約束。"""
        query = """
        CREATE CONSTRAINT user_profile_id_unique IF NOT EXISTS
        FOR (u:User)
        REQUIRE u.user_id IS UNIQUE
        """

        with self.neo4j_driver.session() as session:
            session.run(query).consume()

    def save_profile(
        self,
        user_id: str,
        profile: dict,
    ) -> dict:
        """
        建立或更新 Profile。

        找不到 User 時會建立；
        已存在時會更新同一個 User。
        """
        allergens = list(
            profile.get("allergens", [])
        )

        other_allergen = (
            profile
            .get("otherAllergen", "")
            .strip()
        )

        if (
            other_allergen
            and other_allergen not in allergens
        ):
            allergens.append(other_allergen)

        # 去除重複值與空字串
        allergens = list(
            dict.fromkeys(
                item.strip()
                for item in allergens
                if isinstance(item, str)
                and item.strip()
            )
        )

        query = """
        MERGE (u:User {user_id: $user_id})

        ON CREATE SET
            u.created_at = datetime()

        SET
            u.name = $name,
            u.gender = $gender,
            u.birthday = $birthday,
            u.email = $email,
            u.line_bound = $line_bound,
            u.skin_type = $skin_type,
            u.allergens = $allergens,
            u.customer_type = $customer_type,
            u.wellness_status = $wellness_status,
            u.period_start_date = $period_start_date,
            u.period_end_date = $period_end_date,
            u.updated_at = datetime()

        RETURN {
            user_id: u.user_id,
            name: u.name,
            gender: u.gender,
            birthday: u.birthday,
            email: u.email,
            lineBound: u.line_bound,
            skinType: u.skin_type,
            allergens: u.allergens,
            otherAllergen: "",
            customerType: u.customer_type,
            wellnessStatus: u.wellness_status,
            periodStartDate: u.period_start_date,
            periodEndDate: u.period_end_date
        } AS profile
        """

        parameters = {
            "user_id": user_id,
            "name": profile.get(
                "name",
                "",
            ).strip(),
            "gender": profile.get(
                "gender",
                "",
            ),
            "birthday": profile.get(
                "birthday",
                "",
            ),
            "email": profile.get(
                "email",
                "",
            ).strip().lower(),
            "line_bound": profile.get(
                "lineBound",
                False,
            ),
            "skin_type": profile.get(
                "skinType",
                "",
            ),
            "allergens": allergens,
            "customer_type": profile.get(
                "customerType",
                "",
            ),
            "wellness_status": profile.get(
                "wellnessStatus",
                "",
            ),
            "period_start_date": profile.get(
                "periodStartDate",
                "",
            ),
            "period_end_date": profile.get(
                "periodEndDate",
                "",
            ),
        }

        with self.neo4j_driver.session() as session:
            record = session.run(
                query,
                **parameters,
            ).single()

        if record is None:
            raise RuntimeError(
                "Neo4j 沒有回傳 Profile"
            )

        return dict(record["profile"])

    def get_profile(
        self,
        user_id: str,
    ) -> Optional[dict]:
        """依照 user_id 讀取 Profile。"""
        query = """
        MATCH (u:User {user_id: $user_id})

        RETURN {
            user_id: u.user_id,
            name: coalesce(u.name, ""),
            gender: coalesce(u.gender, ""),
            birthday: coalesce(u.birthday, ""),
            email: coalesce(u.email, ""),
            lineBound: coalesce(
                u.line_bound,
                false
            ),
            skinType: coalesce(
                u.skin_type,
                ""
            ),
            allergens: coalesce(
                u.allergens,
                []
            ),
            otherAllergen: "",
            customerType: coalesce(
                u.customer_type,
                ""
            ),
            wellnessStatus: coalesce(
                u.wellness_status,
                ""
            ),
            periodStartDate: coalesce(
                u.period_start_date,
                ""
            ),
            periodEndDate: coalesce(
                u.period_end_date,
                ""
            )
        } AS profile
        """

        with self.neo4j_driver.session() as session:
            record = session.run(
                query,
                user_id=user_id,
            ).single()

        if record is None:
            return None

        return dict(record["profile"])

    def update_skin_profile(
        self,
        user_id: str,
        skin_type: Optional[str] = None,
        allergens: Optional[list[str]] = None,
    ) -> dict:
        """Partially update stable skin facts learned explicitly in chat."""
        normalized_allergens = None
        if allergens is not None:
            normalized_allergens = list(
                dict.fromkeys(
                    item.strip()
                    for item in allergens
                    if isinstance(item, str) and item.strip()
                )
            )

        query = """
        MERGE (u:User {user_id: $user_id})
        ON CREATE SET u.created_at = datetime()
        SET
            u.skin_type = CASE
                WHEN $set_skin_type THEN $skin_type
                ELSE u.skin_type
            END,
            u.allergens = CASE
                WHEN $set_allergens THEN $allergens
                ELSE u.allergens
            END,
            u.updated_at = datetime()
        """

        with self.neo4j_driver.session() as session:
            session.run(
                query,
                user_id=user_id,
                set_skin_type=skin_type is not None,
                skin_type=skin_type or "",
                set_allergens=allergens is not None,
                allergens=normalized_allergens or [],
            ).consume()

        return self.get_profile(user_id=user_id) or {}

    def update_customer_type(
        self,
        user_id: str,
        customer_type: str,
    ) -> dict:
        """Mirror the latest completed customer quiz into the member profile."""
        query = """
        MERGE (u:User {user_id: $user_id})
        ON CREATE SET u.created_at = datetime()
        SET
            u.customer_type = $customer_type,
            u.updated_at = datetime()
        """
        with self.neo4j_driver.session() as session:
            session.run(
                query,
                user_id=user_id,
                customer_type=str(customer_type or "").strip(),
            ).consume()
        return self.get_profile(user_id=user_id) or {}

    def close(self) -> None:
        """關閉 Neo4j driver。"""
        self.neo4j_driver.close()


# 建立全域 Repository，供 FastAPI 路由共用
profile_repository = ProfileRepository()
