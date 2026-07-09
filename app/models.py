# app/models.py
from sqlalchemy import (
    Column, Integer, String, DateTime, Numeric, Time,
    Float, Boolean, ForeignKey
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import relationship
from .database import Base
import uuid
from sqlalchemy.sql import func
from datetime import datetime, timezone

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(UUID(as_uuid=True), default=uuid.uuid4, unique=True, index=True, nullable=False)
    name = Column(String, nullable=True)
    email = Column(String, unique=True, index=True, nullable=True)
    hashed_password = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_login = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    body_info = relationship("UsersBodyInfo", back_populates="user")
    sleep_data = relationship("UsersRealSleepData", back_populates="user")
    waking_periods = relationship("UsersTargetWakingPeriod", back_populates="user")
    intake_data = relationship("UsersRealTimeIntake", back_populates="user")
    pvt_results = relationship("UsersPVTResults", back_populates="user")
    recommendations = relationship("RecommendationsCaffeine", back_populates="user")
    alertness_data = relationship("AlertnessDataForVisualization", back_populates="user")

class UsersBodyInfo(Base):
    __tablename__ = "users_body_info"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id"), primary_key=True)
    gender = Column(String, nullable=True)
    age = Column(Integer, nullable=True)
    height = Column(Float, nullable=True)
    weight = Column(Float, nullable=True)
    bmi = Column(Float, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="body_info")

class UsersRealSleepData(Base):
    __tablename__ = "users_real_sleep_data"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    sleep_start_time = Column(DateTime(timezone=True), nullable=False)
    sleep_end_time = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    is_active = Column(Boolean, nullable=False, default=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    invalidated_at = Column(DateTime(timezone=True), nullable=True)
    edited_from_id = Column(Integer, ForeignKey("users_real_sleep_data.id"), nullable=True)

    user = relationship("User", back_populates="sleep_data")


class UsersTargetWakingPeriod(Base):
    __tablename__ = "users_target_waking_period"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    target_start_time = Column(DateTime(timezone=True), nullable=False)
    target_end_time = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    is_active = Column(Boolean, nullable=False, default=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    invalidated_at = Column(DateTime(timezone=True), nullable=True)
    edited_from_id = Column(Integer, ForeignKey("users_target_waking_period.id"), nullable=True)

    user = relationship("User", back_populates="waking_periods")


class UsersRealTimeIntake(Base):
    __tablename__ = "users_real_time_intake"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    drink_name = Column(String, nullable=False)
    caffeine_amount = Column(Integer, nullable=False)
    taking_timestamp = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    is_active = Column(Boolean, nullable=False, default=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    invalidated_at = Column(DateTime(timezone=True), nullable=True)
    edited_from_id = Column(Integer, ForeignKey("users_real_time_intake.id"), nullable=True)

    user = relationship("User", back_populates="intake_data")


class UsersPVTResults(Base):
    __tablename__ = "users_pvt_results"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    mean_rt = Column(Float, nullable=False)
    lapses = Column(Integer, nullable=False)
    false_starts = Column(Integer, nullable=False)
    test_at = Column(DateTime(timezone=True), nullable=False)
    device = Column(String, nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    kss_level = Column(Integer, nullable=True)

    user = relationship("User", back_populates="pvt_results")


class RecommendationsCaffeine(Base):
    __tablename__ = "recommendations_caffeine"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    recommended_caffeine_amount = Column(Integer, nullable=False)
    recommended_caffeine_intake_timing = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    source_data_latest_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    run_id = Column(PG_UUID(as_uuid=True), nullable=True)
    user = relationship("User", back_populates="recommendations")


class AlertnessDataForVisualization(Base):
    __tablename__ = "alertness_data_for_visualization"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False)
    awake = Column(Boolean, nullable=False)
    g_PD_rec = Column(Float, nullable=False)
    g_PD_real = Column(Float, nullable=False)
    P0_values = Column(Float, nullable=False)
    P_t_caffeine = Column(Float, nullable=True)  # 睡眠時段可 NULL
    P_t_no_caffeine = Column(Float, nullable=True)
    P_t_real = Column(Float, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    source_data_latest_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User", back_populates="alertness_data")


class UsersParams(Base):
    __tablename__ = "users_params"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id"), primary_key=True)
    m_c = Column(Float, nullable=False, default=1.0)
    k_a = Column(Float, nullable=False, default=1.25)
    k_c = Column(Float, nullable=False, default=0.20)
    trait_alertness = Column(Float, nullable=False, default=0.0)
    p0_value = Column(Float, nullable=False, default=270.0)

    last_trait_update = Column(DateTime(timezone=True), nullable=True)
    last_kc_update = Column(DateTime(timezone=True), nullable=True)

    pvt_count_7d = Column(Integer, nullable=False, default=0)
    pvt_avg_7d = Column(Float, nullable=True)

    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User")


class DeviceHeartRateData(Base):
    __tablename__ = "device_heart_rate_data"

    id = Column(Integer, primary_key=True, index=True)
    time = Column(DateTime(timezone=True), nullable=False)
    heartrate = Column(Integer, nullable=False)
    confidence = Column(Integer, nullable=False)
    source = Column(String, nullable=True)  # 允許為空
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=True)  # 允許為空
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    # user = relationship("User ", back_populates="device_heart_rate_data")  # 暫時不連結


class DeviceXYZTimeData(Base):
    __tablename__ = "device_xyz_time"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime(timezone=True), nullable=False)  # TIMESTAMP WITH TIME ZONE
    x = Column(Float, nullable=False)
    y = Column(Float, nullable=False)
    z = Column(Float, nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
