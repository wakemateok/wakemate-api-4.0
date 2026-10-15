# app/schemas.py
from pydantic import BaseModel, EmailStr, Field
from uuid import UUID
from datetime import date, datetime, time
from typing import List, Optional

# --- 使用者註冊 ---
class UserCreate(BaseModel):
    name: str
    email: EmailStr
    password: str

class UserResponse(BaseModel):
    user_id: UUID
    name: str
    email: EmailStr
    created_at: datetime

    class Config:
        from_attributes = True  # 替代 orm_mode = True

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class UserLoginResponse(BaseModel):
    user_id: UUID
    name: Optional[str]
    email: EmailStr
    last_login: Optional[datetime]

    class Config:
        from_attributes = True

# --- 使用者身體數據(性別、年齡、身高、體重、bmi) ---
class UsersBodyInfoCreate(BaseModel):
    user_id: UUID
    gender: str
    age:int
    height: float
    weight: float
    bmi: float

class UsersBodyInfoResponse(BaseModel):
    user_id: UUID
    gender: Optional[str]
    age: Optional[int]
    height: Optional[float]
    weight: Optional[float]
    bmi: Optional[float]
    updated_at: datetime

    class Config:
        from_attributes = True



# --- 使用者實際睡眠資料 ---
class DayTimePeriod(BaseModel):
    start: datetime
    end: datetime


class ReplaceDayRecords(BaseModel):
    user_id: UUID
    day: date
    periods: List[DayTimePeriod] = Field(min_length=1, max_length=32)


class UsersRealSleepDataCreate(BaseModel):
    user_id: UUID
    sleep_start_time: datetime
    sleep_end_time: datetime

class UsersRealSleepData_DB_Response(BaseModel):
    id: int
    user_id: UUID
    sleep_start_time:  datetime
    sleep_end_time: datetime
    created_at: datetime
    updated_at: datetime
    is_active: bool
    deleted_at: Optional[datetime]
    invalidated_at: Optional[datetime]
    edited_from_id: Optional[int]

    class Config:
        from_attributes = True  # 替代 orm_mode = True

class UsersRealSleepDataUpdate(BaseModel):
    sleep_start_time: datetime
    sleep_end_time: datetime

# API 回傳格式 (新增成功用)
class UsersRealSleepDataCreate_API_Response(BaseModel):
    status: str
    id: int
    calculation: dict  # trigger_calculation 回傳的結果



# --- 使用者設定清醒區間 ---
class UsersTargetWakingPeriodCreate(BaseModel):
    user_id: UUID
    target_start_time:  Optional[datetime]  # ISO 格式時間字串
    target_end_time:  Optional[datetime]

class UsersTargetWakingPeriod_DB_Response(BaseModel):
    id: int
    user_id: UUID
    target_start_time:  Optional[datetime]  # ISO 格式時間字串
    target_end_time:  Optional[datetime]
    created_at: datetime
    updated_at: datetime
    is_active: bool
    deleted_at: Optional[datetime]
    invalidated_at: Optional[datetime]
    edited_from_id: Optional[int]


    class Config:
        from_attributes = True  # 替代 orm_mode = True

class UsersTargetWakingPeriodUpdate(BaseModel):
    target_start_time: datetime
    target_end_time: datetime

# API 回傳格式 (新增成功用)
class UsersTargetWakingPeriodCreate_API_Response(BaseModel):
    status: str
    id: int
    calculation: dict  # trigger_calculation 回傳的結果



# --- 使用者即時咖啡因攝取資料 ---
class UsersRealTimeIntakeCreate(BaseModel):
    user_id: UUID
    drink_name: str
    caffeine_amount: int
    taking_timestamp: datetime

class UsersRealTimeIntake_DB_Response(BaseModel):
    id: int
    user_id: UUID
    drink_name: str
    caffeine_amount: int
    taking_timestamp: datetime
    created_at: datetime
    updated_at: datetime
    is_active: bool
    deleted_at: Optional[datetime]
    invalidated_at: Optional[datetime]
    edited_from_id: Optional[int]


    class Config:
        from_attributes = True  # 替代 orm_mode = True

class UsersRealTimeIntakeUpdate(BaseModel):
    drink_name: str
    caffeine_amount: int
    taking_timestamp: datetime

# API 回傳格式 (新增成功用)
class UsersRealTimeIntakeCreate_API_Response(BaseModel):
    status: str
    id: int
    calculation: dict  # trigger_calculation 回傳的結果



# --- 使用者反應時間測試結果 (PVT) ---
class UsersPVTResultsCreate(BaseModel):
    user_id: UUID
    mean_rt: float
    lapses: int
    false_starts: int
    test_at: datetime
    device: str
    kss_level: Optional[int]

class UsersPVTResults_DB_Response(BaseModel):
    user_id: UUID
    mean_rt: float
    lapses: int
    false_starts: int
    test_at: datetime
    device: str
    kss_level: Optional[int]
    updated_at: datetime

    class Config:
        from_attributes = True  # 替代 orm_mode = True

# API 回傳格式 (新增成功用)
class UsersPVTResultsCreate_API_Response(BaseModel):
    status: str
    id: int
    calculation: dict  # trigger_calculation 回傳的結果



# --- 建議攝取資訊 ---
class RecommendationsCaffeineCreate(BaseModel):
    user_id: UUID
    recommended_caffeine_amount: Optional[int]
    recommended_caffeine_intake_timing: Optional[datetime]

class RecommendationsCaffeine_DB_Response(BaseModel):
    user_id: UUID
    recommended_caffeine_amount: Optional[int]
    recommended_caffeine_intake_timing: Optional[datetime]
    updated_at: datetime
    source_data_latest_at: Optional[datetime]
    is_active: bool
    run_id: Optional[UUID]

    class Config:
        from_attributes = True



# --- 清醒度資料 ---
class AlertnessDataCreate(BaseModel):
    user_id: UUID
    timestamp: datetime
    awake: bool
    g_PD_rec: float
    g_PD_real: float
    P0_values: float
    P_t_caffeine: Optional[float] = None
    P_t_no_caffeine: Optional[float] = None
    P_t_real: Optional[float] = None

class AlertnessData_DB_Response(BaseModel):
    user_id: UUID
    timestamp: datetime
    awake: bool
    g_PD_rec: float
    g_PD_real: float
    P0_values: float
    P_t_caffeine: Optional[float] = None
    P_t_no_caffeine: Optional[float] = None
    P_t_real: Optional[float] = None
    updated_at: datetime
    source_data_latest_at: Optional[datetime]

    class Config:
        from_attributes = True  # 替代 orm_mode = True

# --- Device ------------------------------------------------
from typing import Optional

# ================== Device Heart Rate ==================
class DeviceHeartRateDataCreate(BaseModel):
    time: datetime
    heartrate: int
    confidence: int
    source: Optional[str]
    user_id: Optional[UUID]

class DeviceHeartRate_DB_Response(BaseModel):
    id: int
    time: datetime
    heartrate: int
    confidence: int
    source: Optional[str]
    user_id: Optional[UUID]
    updated_at: datetime

    class Config:
        from_attributes = True


class BulkHeartRate(BaseModel):
    records: List[DeviceHeartRateDataCreate]


# ================== Device XYZ Time ==================
class DeviceXYZTimeDataCreate(BaseModel):
    timestamp: datetime
    x: float
    y: float
    z: float
    user_id: Optional[UUID]

class DeviceXYZTime_DB_Response(BaseModel):
    id: int
    timestamp: datetime
    x: float
    y: float
    z: float
    user_id: Optional[UUID]
    updated_at: datetime

    class Config:
        from_attributes = True

class BulkXYZTime(BaseModel):
    records: List[DeviceXYZTimeDataCreate]
