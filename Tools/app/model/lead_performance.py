from sqlalchemy import Column, String, Text, DateTime, Integer, Float, Boolean, Index, func
from sqlalchemy.dialects.postgresql import JSONB
from pgvector.sqlalchemy import Vector
from datetime import datetime, timedelta, timezone
import datetime
from app.db.database import Base

class LeadPerformanceMetrics(Base):
    __tablename__ = "lead_performance_metrics"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    lead_id = Column(String(100), nullable=False,  index=True)
    sales_executive = Column(String(100), nullable=False, index=True)
    
    # Response Metrics
    avg_response_time_minutes = Column(Float)
    response_count = Column(Integer, default=0)
    response_rate = Column(Float)
    
    # Efficiency Metrics  
    efficiency_score = Column(Float)
    meaningful_followups = Column(Integer, default=0)
    followup_frequency_per_day = Column(Float)
    site_visit_done = Column(Boolean, default=False)
    
    # Engagement Metrics
    engagement_score = Column(Float)
    customer_initiated_calls = Column(Integer, default=0)
    whatsapp_responses = Column(Integer, default=0)
    site_visits_count = Column(Integer, default=0)
    location_shared_count = Column(Integer, default=0)
    conversion_probability = Column(Float)
    
    # Executive Performance Scores
    response_time_score = Column(Float)
    followup_efficiency_score = Column(Float)
    engagement_quality_score = Column(Float)
    overall_lead_score = Column(Float)
    
    # Timestamps
    IST = timezone(timedelta(hours=5, minutes=30))
    now = now = datetime.datetime.now(IST)
    calculated_at = Column(DateTime, default=func.now())

    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

# Index for better query performance
Index('idx_lead_perf_executive', LeadPerformanceMetrics.sales_executive)
Index('idx_lead_perf_score', LeadPerformanceMetrics.overall_lead_score)

class ExecutivePerformanceAggregates(Base):
    __tablename__ = "executive_performance_aggregates"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    executive_name = Column(String(100), nullable=False, index=True)
    period_start = Column(DateTime, nullable=False)
    period_end = Column(DateTime, nullable=False)
    
    # Overall Performance
    performance_score = Column(Float)
    conversion_rate = Column(Float)
    avg_response_time_minutes = Column(Float)
    avg_efficiency_score = Column(Float)
    
    # Lead Counts
    total_leads_handled = Column(Integer, default=0)
    active_leads_count = Column(Integer, default=0)
    converted_leads_count = Column(Integer, default=0)
    
    # Detailed Metrics
    total_customer_calls = Column(Integer, default=0)
    total_site_visits = Column(Integer, default=0)
    total_meaningful_followups = Column(Integer, default=0)
    
    # Timestamps
    calculated_at = Column(DateTime, default=datetime.datetime.utcnow)

# Composite index for period-based queries
Index('idx_exec_agg_period', ExecutivePerformanceAggregates.executive_name, ExecutivePerformanceAggregates.period_start, ExecutivePerformanceAggregates.period_end, unique=True)

class PerformanceSuggestions(Base):
    __tablename__ = "performance_suggestions"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    lead_id = Column(String(100), index=True)
    executive_name = Column(String(100), index=True)
    suggestion_type = Column(String(50))
    suggestion_text = Column(Text, nullable=False)
    priority = Column(String(20))  # 'high', 'medium', 'low'
    is_implemented = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

# Indexes for suggestion queries
Index('idx_suggestions_lead', PerformanceSuggestions.lead_id)
Index('idx_suggestions_executive', PerformanceSuggestions.executive_name)
Index('idx_suggestions_priority', PerformanceSuggestions.priority)