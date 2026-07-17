

import os, sys
import time
# Add project root (FastApi) to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from app.model.lead_details import LeadActivity
from app.model.lead_performance import LeadPerformanceMetrics, PerformanceSuggestions, ExecutivePerformanceAggregates
from sqlalchemy.orm import Session
from app.db.database import SessionLocal, Base
from app.generic.embedding import generate_embedding
import json
import httpx
import asyncio
import datetime
from collections import defaultdict
from datetime import datetime, timedelta
import re
# from sqlalchemy import func
from sqlalchemy import func, case

from app.model.lead_history import LeadHistory

from datetime import datetime, timedelta, timezone
from sqlalchemy import func, case, cast, DateTime
httpx.get("https://generativelanguage.googleapis.com")

HEADERS = {"Authorization": "demo_token_123"}
def filter_followups_last_24h(follow_ups):
    """Filter follow-ups to only include those from the last 24 hours"""
    now = datetime.now(timezone.utc)
    twenty_four_hours_ago = now - timedelta(hours=24)
    
    filtered_followups = []
    for followup in follow_ups:
        try:
            # Parse the update_time string to datetime object
            followup_time = datetime.strptime(followup['update_time'], "%Y-%m-%d %H:%M:%S")
            followup_time = followup_time.replace(tzinfo=timezone.utc)
            
            # Check if followup is within last 24 hours
            if followup_time >= twenty_four_hours_ago:
                filtered_followups.append(followup)
        except Exception as e:
            print(f"⚠️ Error parsing followup time {followup['update_time']}: {e}")
            continue
    
    print(f"🔍 Filtered {len(filtered_followups)}/{len(follow_ups)} follow-ups from last 24h")
    return filtered_followups
def calculate_time_difference(start_time: str, end_time: str) -> float:
    """Calculate time difference in minutes between two timestamps"""
    fmt = "%Y-%m-%d %H:%M:%S"
    start = datetime.strptime(start_time, fmt)
    end = datetime.strptime(end_time, fmt)
    return (end - start).total_seconds() / 60

def calculate_response_metrics(follow_ups):
    """Calculate response times from follow-up data - ONLY LAST 24H"""
    # First filter to only last 24 hours
    recent_followups = filter_followups_last_24h(follow_ups)
    
    response_times = []
    last_incoming_call = None
    
    # Sort follow-ups by time
    sorted_followups = sorted(recent_followups, key=lambda x: x['update_time'])
    
    for followup in sorted_followups:
        # Detect incoming calls from customer
        if (followup['type'] == 'call' and 
            'Incoming' in followup['additional_info'] and
            not followup['additional_info'].startswith('missed')):
            last_incoming_call = followup['update_time']
        
        elif last_incoming_call:
            is_agent_response = (
                (followup['type'] == 'call' and 'Outgoing' in followup['additional_info']) or
                (followup['type'] == 'user' and followup['added_by'] not in ['System'])
            )
            
            if is_agent_response:
                response_time = calculate_time_difference(
                    last_incoming_call, 
                    followup['update_time']
                )
                if response_time > 0:  
                    response_times.append(response_time)
                    last_incoming_call = None
    
    
    total_incoming_calls = len([
        f for f in recent_followups 
        if f['type'] == 'call' and 
        'Incoming' in f['additional_info'] and 
        not f['additional_info'].startswith('missed')
    ])
    
    return {
        "average_response_time_minutes": sum(response_times) / len(response_times) if response_times else None,
        "response_count": len(response_times),
        "response_rate": (len(response_times) / total_incoming_calls * 100) if total_incoming_calls > 0 else 0,
        "total_followups_considered": len(recent_followups)  # For debugging
    }

def calculate_response_time_score(response_metrics):
    
    if not response_metrics['average_response_time_minutes']:
        return 5.0
    
    avg_time = response_metrics['average_response_time_minutes']
    
    if avg_time <= 30:  
        return 10.0
    elif avg_time <= 60:  
        return 8.0
    elif avg_time <= 120:  
        return 6.0
    elif avg_time <= 240: 
        return 4.0
    else:  
        return 2.0

def remove_empty(obj):
   
    if isinstance(obj, dict):
        return {k: remove_empty(v) for k, v in obj.items() if v not in (None, "", "null", [])}
    elif isinstance(obj, list):
        return [remove_empty(v) for v in obj if v not in (None, "", "null")]
    else:
        return obj

def calculate_lead_engagement(follow_ups, lead_data):

    
    recent_followups = filter_followups_last_24h(follow_ups)
    
    engagement_indicators = {
        "customer_initiated_calls": len([
            f for f in recent_followups 
            if f['type'] == 'call' and 
            'Incoming' in f['additional_info'] and
            not f['additional_info'].startswith('missed')
        ]),
        "whatsapp_responses": len([
            f for f in recent_followups 
            if 'WA Done' in f['additional_info'] and 
            'NoReply' not in f['additional_info']
        ]),
        "site_visits": len([
            f for f in recent_followups 
            if 'Site Visit' in f['additional_info']
        ]),
        "location_shared_count": len([
            f for f in recent_followups 
            if 'Location Shared' in f['additional_info']
        ])
    }
    
    total_score = (
        engagement_indicators["customer_initiated_calls"] * 2.0 +
        engagement_indicators["whatsapp_responses"] * 1.5 +
        engagement_indicators["site_visits"] * 4.0 +
        engagement_indicators["location_shared_count"] * 0.5
    )
    
    max_possible_score = 20
    engagement_score = min((total_score / max_possible_score) * 10, 10)
    
    conversion_probability = calculate_conversion_probability(engagement_indicators)
    
    return {
        "engagement_score": round(engagement_score, 1),
        "engagement_breakdown": engagement_indicators,
        "conversion_probability": conversion_probability,
        "recent_followups_count": len(recent_followups)  # For debugging
    }

def calculate_conversion_probability(engagement_indicators):
    """Predict conversion probability based on engagement patterns"""
    base_probability = 15  
    if engagement_indicators["site_visits"] > 0:
        base_probability += 35
    if engagement_indicators["customer_initiated_calls"] > 2:
        base_probability += 25
    if engagement_indicators["whatsapp_responses"] > 0:
        base_probability += 15
    if engagement_indicators["location_shared_count"] > 1:
        base_probability += 10
    
    return min(base_probability, 90)  

def calculate_followup_efficiency(follow_ups, lead_created_date):
    """Calculate efficiency metrics from follow-up data - ONLY LAST 24H"""
    
    recent_followups = filter_followups_last_24h(follow_ups)
    
    meaningful_followups = [
        f for f in recent_followups 
        if f['added_by'] not in ['System'] and f['type'] in ['user', 'call']
    ]
    
    total_recent_followups = len(recent_followups)
    meaningful_count = len(meaningful_followups)
    
    if len(recent_followups) > 1:
        time_span = calculate_time_difference(
            recent_followups[0]['update_time'],
            recent_followups[-1]['update_time']
        )
        # If all followups are within a short period, calculate per hour instead
        if time_span < 1440:  # Less than 24 hours
            followup_frequency = len(recent_followups) / max(time_span / 1440, 0.1)  # per day, min 0.1 day
        else:
            followup_frequency = len(recent_followups) / (time_span / 1440)  # per day
    else:
        followup_frequency = 0
    
    # Check for site visits in recent data
    site_visit_done = any(
        'Site Visit' in f['additional_info'] 
        for f in recent_followups
    )
    
    # Efficiency score calculation based only on recent activity
    efficiency_score = (
        (meaningful_count / total_recent_followups * 0.4 if total_recent_followups > 0 else 0) +
        (min(followup_frequency, 3) / 3 * 0.3) +
        (0.3 if site_visit_done else 0)
    ) * 10
    
    return {
        "efficiency_score": round(efficiency_score, 1),
        "meaningful_followups": meaningful_count,
        "followup_frequency_per_day": round(followup_frequency, 1),
        "site_visit_done": site_visit_done,
        "total_recent_followups": total_recent_followups  # For debugging
    }

def parse_lead_analytics(lead_detail: dict, followups: list) -> dict:
    """Extract analytics data from lead details and followups"""
    analytics = {
        "total_followups": len(followups),
        "followup_types": {},
        "timeline_analysis": {},
        "agent_performance": {}
    }
    
    for followup in followups:
        action_type = classify_followup_type(followup["additional_info"])
        analytics["followup_types"][action_type] = analytics["followup_types"].get(action_type, 0) + 1
    
    if followups:
        first_followup = followups[0]
        last_followup = followups[-1]
        analytics["timeline_analysis"] = {
            "first_activity": first_followup["update_time"],
            "last_activity": last_followup["update_time"],
            "total_duration_days": "calculate_based_on_timestamps"  
        }
    
    agents = {}
    for followup in followups:
        agent = followup["added_by"]
        if agent not in ["System"]:  
            agents[agent] = agents.get(agent, 0) + 1
    
    analytics["agent_performance"] = agents
    
    return analytics

def classify_followup_type(content: str) -> str:
    """Classify followup type"""
    content_lower = content.lower()
    
    if "whatsapp" in content_lower or "msg" in content_lower:
        return "whatsapp_message"
    elif "location" in content_lower and "shared" in content_lower:
        return "location_shared"
    elif "comment" in content_lower:
        return "comment"
    elif "assigned" in content_lower:
        return "assignment"
    elif "ignored" in content_lower:
        return "lead_ignored"
    elif "need in future" in content_lower:
        return "future_interest"
    elif "moved to active" in content_lower:
        return "status_update"
    else:
        return "general"

from datetime import datetime, timedelta

async def store_lead_data(lead_data: dict, db: Session):
   
    try:
        last_24hr = datetime.now() - timedelta(hours=24)  
        lead_details_list = lead_data.get("data", {}).get("lead_data", [])
        
        if not lead_details_list:
            print(" No lead data found in API response")
            return
        
        print(f" Storing {len(lead_details_list)} leads in database...")
        
        stored_count = 0
        for lead_details in lead_details_list:
            try:
                lead_id = lead_details['id']  
                content_parts = []
                lead_info = f"contact:{lead_details.get('contact_details')}, location:{lead_details.get('location')}, status:{lead_details.get('status')},assign_to:{lead_details.get('assign_to')}"
                content_parts.append(lead_info)
                
                followups_data = lead_details.get('follow_up', [])
                recent_followups = []  
                
                # for followup in followups_data:
                #     try:
                #         updated_time = datetime.strptime(followup['update_time'], "%Y-%m-%d %H:%M:%S")
                #         if updated_time >= last_24hr:
                #             followup_text = f"added_by: {followup['added_by']}:comment: {followup['additional_info']} Status: {followup['status']}"
                #             content_parts.append(followup_text)
                #             recent_followups.append(followup)
                #     except Exception as e:
                #         print(f" Error parsing followup date: {e}")
                #         continue
                
                combined_content = "\n".join(content_parts)

                added_on = None
                closed_on = None
                try:
                    if lead_details.get("added_on"):
                        added_on = datetime.strptime(lead_details.get("added_on"), "%Y-%m-%d %H:%M:%S")
                    if lead_details.get("closed_on"):
                        closed_on = datetime.strptime(lead_details.get("closed_on"), "%Y-%m-%d %H:%M:%S")
                except Exception as e:
                    print(f"⚠️ Error parsing dates for lead {lead_id}: {e}")
                
                lead_activity = LeadActivity(
                    lead_id=lead_id,
                    customer_phone=lead_details.get("contact_details"),
                    customer_phone2=lead_details.get("contact_details2"),
                    customer_email=lead_details.get("email_id"),
                    location=lead_details.get("location"),
                    origin=lead_details.get("origin"),
                    status=lead_details.get("status"),
                    assigned_to=lead_details.get("assign_to"),
                    added_by=lead_details.get("added_by"),
                    added_on=added_on,
                    closed_on=closed_on,
                    last_updated_by=lead_details.get("last_updated_by"),
                    followups={"items": followups_data},  
                    content=" ",
                    # extracted_data=parse_lead_analytics(lead_details, recent_followups)
                )
                
                db.merge(lead_activity)
                stored_count += 1
                
                if stored_count % 10 == 0:
                    print(f"📊 Progress: Stored {stored_count}/{len(lead_details_list)} leads")
                    
            except Exception as e:
                print(f"❌ Error storing lead {lead_details.get('id', 'unknown')}: {e}")
                continue
        
        db.commit()
        print(f"✅ Successfully stored {stored_count} leads in database")
        
    except Exception as e:
        print(f"❌ Error in store_lead_data: {e}")
        db.rollback()
        raise


async def update_executive_performance_aggregates(db: Session):
    """✅ Calculate and store individual executive performance for current 24-hour period"""
    print("📊 Calculating executive performance for current 24-hour period...")

    IST = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(IST)
    print(now)
    
    period_end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)  # Start of next day
    period_start = period_end - timedelta(hours=24)  # Start of current day
    
    print(f"  Time period: {period_start} to {period_end}")

    executives = db.query(LeadPerformanceMetrics.sales_executive).filter(
        LeadPerformanceMetrics.updated_at >= period_start,
        LeadPerformanceMetrics.updated_at <= period_end,
        LeadPerformanceMetrics.sales_executive.isnot(None)
    ).distinct().all()

    print(f"  Found {len(executives)} active executives")

    for (executive_name,) in executives:
        if not executive_name or executive_name.strip() == "":
            continue

        try:
            existing_record = db.query(ExecutivePerformanceAggregates).filter(
                ExecutivePerformanceAggregates.executive_name == executive_name,
                ExecutivePerformanceAggregates.period_start == period_start,
                ExecutivePerformanceAggregates.period_end == period_end
            ).first()

            # Calculate aggregates for THIS executive for THIS period
            aggregates = db.query(
                func.avg(LeadPerformanceMetrics.overall_lead_score).label('avg_score'),
                func.avg(LeadPerformanceMetrics.conversion_probability).label('avg_conversion_rate'),
                func.avg(LeadPerformanceMetrics.avg_response_time_minutes).label('avg_response_time'),
                func.avg(LeadPerformanceMetrics.efficiency_score).label('avg_efficiency'),
                func.count(LeadPerformanceMetrics.lead_id).label('total_leads'),
                func.sum(case((LeadActivity.status == 'converted', 1), else_=0)).label('converted_leads'),
                func.sum(LeadPerformanceMetrics.customer_initiated_calls).label('total_customer_calls'),
                func.sum(LeadPerformanceMetrics.site_visits_count).label('total_site_visits'),
                func.sum(LeadPerformanceMetrics.meaningful_followups).label('total_meaningful_followups')
            ).join(
                LeadActivity, LeadActivity.lead_id == LeadPerformanceMetrics.lead_id
            ).filter(
                LeadPerformanceMetrics.sales_executive == executive_name,
                LeadPerformanceMetrics.updated_at >= period_start,
                LeadPerformanceMetrics.updated_at <= period_end
            ).first()

            # Active leads count for THIS executive for THIS period
            active_leads = db.query(LeadActivity).filter(
                LeadActivity.assigned_to == executive_name,
                LeadActivity.status.in_(['active', 'in_progress', 'contacted']),
                cast(LeadActivity.last_updated_by, DateTime) >= period_start
            ).count()

            if existing_record:
                # ✅ UPDATE existing record for this executive and period
                existing_record.performance_score = round(aggregates.avg_score or 0, 2)
                existing_record.conversion_rate = round(aggregates.avg_conversion_rate or 0, 2)
                existing_record.avg_response_time_minutes = round(aggregates.avg_response_time or 0, 2)
                existing_record.avg_efficiency_score = round(aggregates.avg_efficiency or 0, 2)
                existing_record.total_leads_handled = aggregates.total_leads or 0
                existing_record.active_leads_count = active_leads
                existing_record.converted_leads_count = aggregates.converted_leads or 0
                existing_record.total_customer_calls = aggregates.total_customer_calls or 0
                existing_record.total_site_visits = aggregates.total_site_visits or 0
                existing_record.total_meaningful_followups = aggregates.total_meaningful_followups or 0
                existing_record.calculated_at = now
                
                print(f" 🔄 Updated performance for {executive_name} ({period_start.date()})")
            else:
                exec_aggregate = ExecutivePerformanceAggregates(
                    executive_name=executive_name,
                    period_start=period_start,
                    period_end=period_end,
                    performance_score=round(aggregates.avg_score or 0, 2),
                    conversion_rate=round(aggregates.avg_conversion_rate or 0, 2),
                    avg_response_time_minutes=round(aggregates.avg_response_time or 0, 2),
                    avg_efficiency_score=round(aggregates.avg_efficiency or 0, 2),
                    total_leads_handled=aggregates.total_leads or 0,
                    active_leads_count=active_leads,
                    converted_leads_count=aggregates.converted_leads or 0,
                    total_customer_calls=aggregates.total_customer_calls or 0,
                    total_site_visits=aggregates.total_site_visits or 0,
                    total_meaningful_followups=aggregates.total_meaningful_followups or 0,
                    calculated_at=now
                )
                
                db.add(exec_aggregate)
                print(f"    ✅ Created performance for {executive_name} ({period_start.date()})")

            db.commit()

        except Exception as e:
            print(f" Error processing {executive_name}: {e}")
            db.rollback()
            continue

    print(f"🎯 Executive performance updated for {len(executives)} executives for {period_start.date()}!")

    
async def store_lead_performance_metrics(lead_data: dict, db: Session):
    """Calculate and store performance metrics for ALL leads - UPDATED FOR 24H FILTER"""
    try:
        lead_details_list = lead_data.get("data", {}).get("lead_data", [])
        print(f" Calculating performance metrics for {len(lead_details_list)} leads (LAST 24H ONLY)...")

        for lead_details in lead_details_list:
            lead_id = lead_details.get("id")

            try:
                follow_ups = lead_details.get("follow_up", [])
                if not follow_ups:
                    print(f" Skipping lead {lead_id} - no follow-ups")
                    continue

                executive_name = lead_details.get("assign_to")
                if not executive_name:
                    print(f" Skipping lead {lead_id} - no executive assigned")
                    continue

                recent_followups = filter_followups_last_24h(follow_ups)
                if not recent_followups:
                    print(f" Skipping lead {lead_id} - no follow-ups in last 24h")
                    continue

                
                response_metrics = calculate_response_metrics(follow_ups)
                efficiency_metrics = calculate_followup_efficiency(follow_ups, lead_details['added_on'])
                engagement_metrics = calculate_lead_engagement(follow_ups, lead_details)

                response_time_score = calculate_response_time_score(response_metrics)
                overall_lead_score = round((
                    response_time_score * 0.3 +
                    efficiency_metrics['efficiency_score'] * 0.4 +
                    engagement_metrics['engagement_score'] * 0.3
                ), 1)

                performance_metrics = LeadPerformanceMetrics(
                    lead_id=lead_id,
                    sales_executive=executive_name,
                    avg_response_time_minutes=response_metrics['average_response_time_minutes'],
                    response_count=response_metrics['response_count'],
                    response_rate=round(response_metrics['response_rate'], 2),
                    efficiency_score=efficiency_metrics['efficiency_score'],
                    meaningful_followups=efficiency_metrics['meaningful_followups'],
                    followup_frequency_per_day=efficiency_metrics['followup_frequency_per_day'],
                    site_visit_done=efficiency_metrics['site_visit_done'],
                    engagement_score=engagement_metrics['engagement_score'],
                    customer_initiated_calls=engagement_metrics['engagement_breakdown']['customer_initiated_calls'],
                    whatsapp_responses=engagement_metrics['engagement_breakdown']['whatsapp_responses'],
                    site_visits_count=engagement_metrics['engagement_breakdown']['site_visits'],
                    location_shared_count=engagement_metrics['engagement_breakdown']['location_shared_count'],
                    conversion_probability=engagement_metrics['conversion_probability'],
                    overall_lead_score=overall_lead_score,
                )

                db.merge(performance_metrics)

                try:
                    db.commit()  
                    print(f" Stored 24h metrics for lead {lead_id}")
                except Exception as e:
                    print(f" Commit failed for lead {lead_id}: {e}")
                    db.rollback()
                    continue

            except Exception as e:
                print(f" Error calculating 24h metrics for lead {lead_id}: {e}")
                db.rollback()
                continue

    except Exception as e:
        print(f" Error in store_lead_performance_metrics: {e}")
        db.rollback()
        raise

    
    await update_executive_performance_aggregates(db)
async def store_improvement_suggestions(lead_id, executive_name, response_metrics, efficiency_metrics, engagement_metrics, db):
    """Generate and store improvement suggestions - FIXED VERSION"""
    try:
        suggestions = []
        
        
        if response_metrics['average_response_time_minutes'] and response_metrics['average_response_time_minutes'] > 60:
            suggestions.append({
                "type": "response_time",
                "text": " Improve response time - aim for under 60 minutes",
                "priority": "high"
            })
        
        # Efficiency suggestions
        if efficiency_metrics['efficiency_score'] < 6:
            suggestions.append({
                "type": "efficiency", 
                "text": " Increase meaningful follow-ups - reduce system-generated updates",
                "priority": "medium"
            })
        
        # Engagement suggestions
        if engagement_metrics['engagement_score'] < 5:
            suggestions.append({
                "type": "engagement",
                "text": " Boost engagement - try different communication channels",
                "priority": "medium"
            })
        
        if not efficiency_metrics['site_visit_done'] and engagement_metrics['conversion_probability'] > 40:
            suggestions.append({
                "type": "conversion",
                "text": "Schedule site visit - increases conversion probability by 40%",
                "priority": "high"
            })
        
        for suggestion in suggestions:
            existing = db.query(PerformanceSuggestions).filter(
                PerformanceSuggestions.lead_id == lead_id,
                PerformanceSuggestions.suggestion_type == suggestion["type"]
            ).first()
            
            if not existing:
                suggestion_record = PerformanceSuggestions(
                    lead_id=lead_id,
                    executive_name=executive_name,
                    suggestion_type=suggestion["type"],
                    suggestion_text=suggestion["text"],
                    priority=suggestion["priority"],
                    created_at=datetime.utcnow()
                )
                db.add(suggestion_record)
                print(f"  Added suggestion: {suggestion['text']}")
        
                
    except Exception as e:
        print(f" Error storing suggestions for lead {lead_id}: {e}")

async def verify_data_storage(db: Session):
    """Verify that data is being stored in all tables"""
    print("\n🔍 VERIFYING DATA STORAGE...")
   
    metrics_count = db.query(LeadPerformanceMetrics).count()
    print(f" LeadPerformanceMetrics records: {metrics_count}")
    
    exec_agg_count = db.query(ExecutivePerformanceAggregates).count()
    print(f"👥 ExecutivePerformanceAggregates records: {exec_agg_count}")
    
    suggestions_count = db.query(PerformanceSuggestions).count()
    print(f"💡 PerformanceSuggestions records: {suggestions_count}")
    
    if exec_agg_count > 0:
        recent_aggregates = db.query(ExecutivePerformanceAggregates).order_by(
            ExecutivePerformanceAggregates.calculated_at.desc()
        ).limit(3).all()
        
        print("\n📈 RECENT EXECUTIVE AGGREGATES:")
        for agg in recent_aggregates:
            print(f"  {agg.executive_name}: Score {agg.performance_score:.1f}, {agg.converted_leads_count} conversions")


def cleanup_individual_executive_duplicates(db: Session):
    """Clean up duplicate executive records - keep only the most recent one per executive"""
    print("🧹 Cleaning up duplicate individual executive records...")
    
    duplicates = db.query(
        ExecutivePerformanceAggregates.executive_name,
        func.count('*').label('count')
    ).group_by(
        ExecutivePerformanceAggregates.executive_name
    ).having(func.count('*') > 1).all()
    
    cleaned_count = 0
    for exec_name, count in duplicates:
        print(f"  Cleaning up {exec_name} - {count} records")
        
        latest_record = db.query(ExecutivePerformanceAggregates).filter(
            ExecutivePerformanceAggregates.executive_name == exec_name
        ).order_by(ExecutivePerformanceAggregates.calculated_at.desc()).first()
        
        if latest_record:
            deleted_count = db.query(ExecutivePerformanceAggregates).filter(
                ExecutivePerformanceAggregates.executive_name == exec_name,
                ExecutivePerformanceAggregates.id != latest_record.id
            ).delete()
            cleaned_count += deleted_count
    
    db.commit()
    print(f"✅ Cleaned up {cleaned_count} duplicate executive records!")
from sqlalchemy import text
def truncate_daily_performance_data(db:Session):
    """Truncate all performance-related tables"""
    db = SessionLocal()
    try:
        db.execute(text("TRUNCATE TABLE lead_performance_metrics RESTART IDENTITY CASCADE;"))
        db.execute(text("TRUNCATE TABLE executive_performance_aggregates RESTART IDENTITY CASCADE;"))
        db.execute(text("TRUNCATE TABLE performance_suggestions RESTART IDENTITY CASCADE;"))
        db.execute(text("TRUNCATE TABLE lead_activities_details RESTART IDENTITY CASCADE;"))
        db.commit()
        print("✅ Successfully truncated all performance-related tables!")
    except Exception as e:
        db.rollback()
        print(f"❌ Error truncating tables: {e}")
    finally:
        db.close()

async def lead():
    """Fetch all leads for a specific date and process them - ENHANCED VERSION"""
    print("🚀 Starting bulk lead processing...")
    db = SessionLocal()
    # truncate_daily_performance_data(db)
   
    # target_date = datetime.now().strftime("%Y-%m-%d")
    url = f"https://www.rentmystay.com/T/all_lead_data_timestamp/2025-11-01/null/null/sales"
    # url = f"https://www.rentmystay.com/T/all_lead_data_timestamp/null/null/364436"
    
    try:
        async with httpx.AsyncClient(timeout=30) as client:
           
            resp = await client.get(url, headers=HEADERS)
            
            if resp.status_code != 200:
                
                print(f"Response: {resp.text}")
                return None
            
            lead_data = resp.json()
            lead_count = len(lead_data.get('data', {}).get('lead_data', []))
            print(f"📥 Received API response with {lead_count} leads")
            
            if not lead_data or not lead_data.get("data") or not lead_data["data"].get("lead_data"):
                print(" No lead data found in API response")
                return {"message": "No lead data found", "lead_data": {}}
            
            # db = SessionLocal()
            try:
                print(f"🔄 Processing {lead_count} leads from API...")
                
                # truncate_daily_performance_data(db)
                
                await store_lead_data(lead_data, db)
                # await store_lead_performance_metrics(lead_data, db)
                
                # await verify_data_storage(db)
                
                return {
                    "message": f"Successfully processed {lead_count} leads",
                    "leads_processed": lead_count,
                    "lead_data_sample": remove_empty(lead_data)
                }
                        
            except Exception as e:
                print(f"❌ Error processing leads: {e}")
                import traceback
                traceback.print_exc()
                db.rollback()
                return {"error": str(e)}
            finally:
                db.close()
                
    except Exception as e:
        print(f"❌ Error in lead function: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}

def main():
    """Main function to run the lead processing"""
    print("=" * 60)
    print("🏠 RENTMYSTAY LEAD PROCESSING SYSTEM")
    print("=" * 60)
    
    result = asyncio.run(lead())
    
    print("=" * 60)
    if result and "error" not in result:
        print("✅ Lead processing completed successfully!")
        print(f"📊 Result: {result.get('message', 'Unknown')}")
        print(f"📈 Leads processed: {result.get('leads_processed', 0)}")
    else:
        print("❌ Lead processing failed!")
        print(f"Error: {result.get('error', 'Unknown error')}")
    print("=" * 60)

if __name__ == "__main__":
    main()
from datetime import datetime, timedelta, timezone
from sqlalchemy import and_
from app.db.database import SessionLocal
from app.model.lead_details import LeadActivity
from app.model.lead_performance import ExecutivePerformanceAggregates

async def sales_executive_lead_per(executive_name: str):
    try:
        now = datetime.now(timezone.utc)
        last_48h = now - timedelta(hours=48)
        db = SessionLocal()
        
        lead_data = db.query(LeadActivity).filter(
            LeadActivity.assigned_to == executive_name
        ).all()

        lead_list = []
        for lead in lead_data:
            
            recent_followups = []
            total_followups = 0
            
            if lead.followups and isinstance(lead.followups, dict):
                items = lead.followups.get('items', [])
                total_followups = len(items)
                
                if isinstance(items, list):
                    for followup in items:
                        if isinstance(followup, dict) and followup.get("update_time"):
                            try:
                                update_dt = datetime.strptime(followup["update_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                                if update_dt >= last_48h:
                                    recent_followups.append(followup)
                            except Exception:
                                continue

            lead_list.append({
                "lead_id": lead.lead_id,
                "customer_phone": lead.customer_phone,
                "customer_phone2": lead.customer_phone2,
                "customer_email": lead.customer_email,
                "location": lead.location,
                "origin": lead.origin,
                "status": lead.status,
                "assigned_to": lead.assigned_to,
                "added_on": lead.added_on.isoformat() if lead.added_on else None,
                "closed_on": lead.closed_on.isoformat() if lead.closed_on else None,
                "followups": recent_followups, 
            })
        print("lead size",len( lead_data))
        # Executive performance data
        current_period_start = now - timedelta(hours=56)
        exec_perf = db.query(ExecutivePerformanceAggregates).filter(
            and_(
                ExecutivePerformanceAggregates.executive_name == executive_name,
                ExecutivePerformanceAggregates.period_start >= current_period_start
            )
        ).order_by(ExecutivePerformanceAggregates.period_start.desc()).first()

        if exec_perf:
            perf_data = {
                "executive_name": exec_perf.executive_name,
                "performance_score": exec_perf.performance_score,
                "conversion_rate": exec_perf.conversion_rate,
                "avg_response_time_minutes": exec_perf.avg_response_time_minutes,
                "total_leads_handled": exec_perf.total_leads_handled,
                "active_leads_count":exec_perf.active_leads_count,
                "total_meaningful_followups":exec_perf.total_meaningful_followups,
                "total_site_visits":exec_perf.total_site_visits,
                "period_start": exec_perf.period_start.isoformat(),
                "period_end": exec_perf.period_end.isoformat(),
              
            }
        else:
            perf_data = {}

        perf_data = {
            "performance_score": exec_perf.performance_score if exec_perf else None,
            "conversion_rate": exec_perf.conversion_rate if exec_perf else None,
            "avg_response_time_minutes": exec_perf.avg_response_time_minutes if exec_perf else None,
            "total_leads_handled": exec_perf.total_leads_handled if exec_perf else None,
        }

        return {
            "lead_details":lead_list,
            "performance":perf_data

        }

    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        db.close()



from sqlalchemy import or_
from app.model.audio_file_model import AudioFile
from app.model.message import Message
from app.generic.generic_utils import api_fetcher, whatsapp_msg_formatter, call_logs_db, call_log_api

async def sales_executive_lead(executive_name: str):
    """
    Fetch all leads, WhatsApp messages, call logs, and browsing history for a given sales executive.
    Now uses standardized utility functions from app.generic.generic_utils.
    """
    try:
        started_time = time.time()
        now = datetime.now(timezone.utc)
        db = SessionLocal()

        # Fetch all leads assigned to this executive
        leads = db.query(LeadActivity).filter(
            LeadActivity.assigned_to == executive_name
        ).limit(6).all()

        if not leads:
            return {"data": [], "message": "No leads found for this executive."}

        all_lead_blocks = []

        for lead in leads:
            content_parts = []
            lead_id = lead.lead_id
            phone = lead.customer_phone

            if not phone:
                continue

            phone = phone.strip()
            short_phone = phone[-10:] if len(phone) > 10 else phone

            # ---- Lead CRM Section ----
            lead_header = (
                f"LeadID: {lead_id}: cx_no:{phone}, "
                f"loc:{lead.location or 'None'}, "
                f"status:{lead.status}, "
                f"sales_team:{lead.assigned_to}"
            )
            content_parts.append(lead_header)
            content_parts.append("")
            content_parts.append(lead.content or "")
            content_parts.append("")

            # ---- WhatsApp Messages ----
            try:
                whatsapp_parts = await whatsapp_msg_formatter(phone)
                if whatsapp_parts:
                    content_parts.append("WhatsApp Messages:")
                    content_parts.extend(whatsapp_parts)
                else:
                    content_parts.append("WhatsApp Messages: No WhatsApp activity found.")
            except Exception as e:
                print(f"Error fetching WhatsApp for {lead_id}: {e}")
                content_parts.append(f"WhatsApp Messages: Error fetching messages ({e})")

            content_parts.append("")

            # ---- Call Logs ----
            try:
                call_logs = await call_logs_db(phone)
                if call_logs and call_logs.get("format_content"):
                    content_parts.append("Sales Call Logs (from DB):")
                    content_parts.extend(call_logs["format_content"])
                else:
                    print(f"No DB logs found, fetching from API for {phone}")
                    call_api_data = await call_log_api(phone)
                    if call_api_data and call_api_data.get("format_content"):
                        content_parts.append("Sales Call Logs (from API):")
                        content_parts.extend(call_api_data["format_content"])
                    else:
                        content_parts.append("Sales Call Logs: No data found.")
            except Exception as e:
                print(f"Error fetching Call Logs for {lead_id}: {e}")
                content_parts.append(f"Sales Call Logs: Error fetching call logs ({e})")

            # ---- Browsing History ----
            try:
                browsing_history = (
                    db.query(LeadHistory)
                    .filter(LeadHistory.lead_id == lead_id)
                    .order_by(LeadHistory.timestamp.asc())
                    .all()
                )

                if browsing_history:
                    content_parts.append("\nBrowsing history (Lead_History from NeonDB):")
                    for record in browsing_history:
                        time_str = (
                            record.timestamp.strftime("%Y-%m-%d %H:%M:%S")
                            if record.timestamp else "Unknown"
                        )
                        log_entry = (
                            f"Lead ID: {lead_id} | Time: {time_str} | Page: {record.current_page or 'N/A'}"
                        )
                        content_parts.append(log_entry)

                else:
                    uri = f"https://www.rentmystay.com/T/customer_browsing_history_bylead/{lead_id}"
                    print(f"No browsing history found in DB. Fetching from API: {uri}")

                    res_json = await api_fetcher(uri)

                    if (
                        isinstance(res_json, dict)
                        and res_json.get("msg") == "Success"
                        and "data" in res_json
                        and "lastVisited_details_of_lead" in res_json["data"]
                    ):
                        browsing_history_data = res_json["data"]["lastVisited_details_of_lead"]
                        if browsing_history_data:
                            content_parts.append("\nBrowsing history (from API):")
                            for item in browsing_history_data:
                                timestamp = item.get("timestamp")
                                current_page = item.get("current_page")
                                content_parts.append(
                                    f"Lead ID: {lead_id} | Time: {timestamp} | Page: {current_page}"
                                )

                                # Store into DB
                                new_record = LeadHistory(
                                    lead_id=lead_id,
                                    current_page=current_page,
                                    timestamp=datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
                                    if timestamp else datetime.utcnow(),
                                )
                                db.add(new_record)
                            db.commit()
                        else:
                            content_parts.append(f"ℹNo browsing history found for lead {lead_id}")
                    else:
                        content_parts.append(f"Browsing history API failed or returned invalid response.")
            except Exception as e:
                print(f"Error fetching browsing history for {lead_id}: {e}")
                content_parts.append(f"Browsing History: Error fetching ({e})")

            
            final_output = "\n".join(content_parts)
            print(final_output)
            all_lead_blocks.append(final_output)

        print(f"sales_executive_lead executed in {time.time() - started_time:.2f}s")
        return {"data": all_lead_blocks}

    except Exception as e:
        print(f"Error in sales_executive_lead: {e}")
        return {"success": False, "error": str(e)}

    finally:
        db.close()





# if __name__ == "__main__":
#     asyncio.run(sales_executive_lead("harish99"))



    
from app.model.call_log import CallLog
from app.model.message import Message
import time
from datetime import datetime, timedelta, timezone
from sqlalchemy import and_
from app.db.database import SessionLocal
from app.generic.llm_ans import gemini
from dotenv import load_dotenv
import redis.asyncio as aioredis
import os
import asyncio
import json
import re
from typing import List, Dict, Any

load_dotenv()

#Chnage localhost to redis later
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")

# REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"
REDIS_URL = os.getenv("REDIS")

redis_client = aioredis.from_url(
    REDIS_URL,
    encoding="utf-8",
    decode_responses=True
)



def estimate_tokens(text: str) -> int:
    return len(text) // 4

def chunk_text(text: str, max_tokens: int):
    approx_chars = max_tokens * 4
    return [text[i:i + approx_chars] for i in range(0, len(text), approx_chars)]

def clean_json_markdown(text: str) -> str:
    """Removes Markdown fences like ```json ... ``` from LLM responses."""
    if not isinstance(text, str):
        return text
    return re.sub(r"```(?:json)?|```", "", text).strip()



async def sales_executive_lead_per(executive_name: str):
    """Fetch all lead data, CRM content, WhatsApp messages, and call logs for an executive."""
    try:
        started_time = time.time()
        now = datetime.now(timezone.utc)
        db = SessionLocal()

        leads = db.query(LeadActivity).filter(
            LeadActivity.assigned_to == executive_name
        ).all()

        if not leads:
            return {"data": "", "message": "No leads found for this executive."}

        all_lead_blocks = []

        for lead in leads:
            content_parts = []
            lead_id = lead.lead_id
            phone = lead.customer_phone

            if not phone:
                continue

            phone = phone.strip()
            short_phone = phone[-10:] if len(phone) > 10 else phone

            lead_header = (
                f"Below is the lead of sales person\n"
                f"LeadID: {lead_id}: cx_no:{phone}, loc:{lead.location or 'None'}, "
                f"status:{lead.status}, sales_team:{lead.assigned_to}"
            )
            content_parts.append(lead_header)
            content_parts.append("")

            # CRM Content
            content_parts.append("Lead status extraction from CRM:")
            content_parts.append(lead.content or "No CRM data available.")
            content_parts.append("")

            # WhatsApp Messages
            try:
                all_messages = (
                    db.query(Message)
                    .filter(
                        (Message.cx_number.like(f"%{short_phone}%"))
                        | (Message.cx_number.like(f"%{phone}%"))
                    )
                    .order_by(Message.timestamp.asc())
                    .all()
                )

                if all_messages:
                    content_parts.append("WhatsApp Messages:")
                    messages_by_admin = {}

                    for msg in all_messages:
                        admin_num = msg.admin_number
                        messages_by_admin.setdefault(admin_num, []).append(msg)

                    for admin_number, messages in messages_by_admin.items():
                        start_time = messages[0].timestamp.strftime("%Y-%m-%d %H:%M")
                        end_time = messages[-1].timestamp.strftime("%Y-%m-%d %H:%M")
                        content_parts.append(
                            f"Admin {admin_number} → Customer {phone} "
                            f"(Conversation window: {start_time} → {end_time})"
                        )

                        for msg in messages:
                            direction = "→" if msg.direction == "outgoing" else "←"
                            if msg.clean_content:
                                content_parts.append(f"{direction} {msg.clean_content.strip()}")

                        content_parts.append("")
                else:
                    content_parts.append("WhatsApp Messages: No WhatsApp activity found.")
            except Exception as e:
                content_parts.append(f"WhatsApp Messages: Error fetching messages ({e})")

            all_lead_blocks.append("\n".join(content_parts))

        formatted_text = "\n".join(all_lead_blocks)

        current_period_start = now - timedelta(hours=56)
        exec_perf = db.query(ExecutivePerformanceAggregates).filter(
            and_(
                ExecutivePerformanceAggregates.executive_name == executive_name,
                ExecutivePerformanceAggregates.period_start >= current_period_start
            )
        ).order_by(ExecutivePerformanceAggregates.period_start.desc()).first()

        perf_data = {
            "performance_score": exec_perf.performance_score if exec_perf else 0,
            "conversion_rate": exec_perf.conversion_rate if exec_perf else 0,
            "avg_response_time_minutes": exec_perf.avg_response_time_minutes if exec_perf else 0,
            "total_leads_handled": exec_perf.total_leads_handled if exec_perf else 0,
            "active_leads_count": exec_perf.active_leads_count if exec_perf else 0,
            "total_meaningful_followups": exec_perf.total_meaningful_followups if exec_perf else 0,
        }

        print(f"✅ Processed {len(leads)} leads in {time.time() - started_time:.2f}s")

        return {"data": formatted_text, "performance": perf_data}

    except Exception as e:
        print(f"❌ Error in sales_executive_lead: {e}")
        return {"success": False, "error": str(e)}

    finally:
        db.close()



def build_reasoning_prompt(executive_name: str, summarized_text: str, whatsapp_text: str = "[No WhatsApp messages found]") -> str:
    return f"""
You are an AI Sales Performance & Behavior Analyst.

Analyze salesperson '{executive_name}' using BOTH:
1. Lead summary and follow-up performance
2. WhatsApp communication (if available)

Your response must be clean JSON only:
{{
  "executive": "{executive_name}",
  "overall_score": <float>,
  "whatsapp_score": <float>,
  "strengths": [<list>],
  "weaknesses": [<list>],
  "suggestions": [<list>],
  "high_alert": <true/false>
}}

Rules:
- Evaluate professionalism, clarity, and follow-up discipline.
- WhatsApp score reflects tone, clarity, and professionalism.
- Mark high_alert true **only** if explicit abuse, threats, or rude language found.
- Ignore 'Ignored' or 'Inactive' statuses for high_alert.
- Keep JSON short and factual.
- No markdown or commentary.

--- LEAD SUMMARY ---
{summarized_text}

--- WHATSAPP CONVERSATIONS ---
{whatsapp_text}
"""

async def analyze_executive_with_reasoning(executive_name: str, max_tokens: int = 15000) -> Dict[str, Any]:
    cache_key = f"sales_reason:{executive_name}"

    # Fetch from DB
    lead_db = await sales_executive_lead_per(executive_name)
    if not lead_db:
        return {"error": f"No lead data for {executive_name}"}

    summarized_text = lead_db["data"]
    prompt = build_reasoning_prompt(executive_name, summarized_text)

    token_count = estimate_tokens(prompt)
    print(f"Estimated tokens for {executive_name}: {token_count}")

    # Handle chunking
    if token_count > max_tokens:
        chunks = chunk_text(summarized_text, max_tokens)
        partial_responses = []

        for idx, chunk in enumerate(chunks, start=1):
            print(f"Processing chunk {idx}/{len(chunks)} for {executive_name}...")
            chunk_prompt = build_reasoning_prompt(
                executive_name,
                f"(chunk {idx}/{len(chunks)})\n\n{chunk}"
            )
            resp = await gemini(chunk_prompt, "")
            partial_responses.append(clean_json_markdown(resp))
            await asyncio.sleep(1.2)

        combined_text = "\n\n".join(partial_responses)
        final_prompt = f"""
Combine these analyses for '{executive_name}' into one clean JSON:
{{
  "executive": "{executive_name}",
  "overall_score": <float>,
  "whatsapp_score": <float>,
  "strengths": [<list>],
  "weaknesses": [<list>],
  "suggestions": [<list>],
  "high_alert": <true/false>
}}

Ensure all scores are integers between 1 and 10.
No markdown, no reasoning.
Partial analyses:
{combined_text}
"""
        raw_final = await gemini(final_prompt, "")
    else:
        raw_final = await gemini(prompt, "")

    # Clean and parse output
    clean_output = clean_json_markdown(raw_final)
    try:
        parsed_json = json.loads(clean_output)
    except Exception:
        parsed_json = {"executive": executive_name, "raw_text": clean_output}

    # Normalize scores
    def normalize_score(value):
        try:
            val = float(value)
            if val <= 1.5:  # LLM likely output 0–1.5 range
                return round(val * 6.5, 2)  # map 0–1.5 → 0–10
            return round(min(val, 10.0), 2)
        except Exception:
            return None

    if "overall_score" in parsed_json:
        parsed_json["overall_score"] = normalize_score(parsed_json["overall_score"])
    if "whatsapp_score" in parsed_json:
        parsed_json["whatsapp_score"] = normalize_score(parsed_json["whatsapp_score"])

    # Accurate WhatsApp status
    whatsapp_data_found = "WhatsApp Messages:" in summarized_text
    if not whatsapp_data_found:
        parsed_json["whatsapp_status"] = "No WhatsApp messages found in DB"
    else:
        parsed_json["whatsapp_status"] = (
            "WhatsApp messages analyzed successfully" 
            if parsed_json.get("whatsapp_score", 0) > 0 
            else "WhatsApp messages found but poor engagement"
        )

    print(f"✅ Completed analysis for {executive_name}")
    return parsed_json


# 🔹 Analyze all salespersons
async def sales_team_24hr():
    SALES_EXECUTIVES = [
        "Sagarikanoatia905",
        "harish99",
        "abbas24042000",
        "ashwathianair",
        "hari.kattamanchi",
    ]
    results = {}
    for name in SALES_EXECUTIVES:

        print(f"Analyzing salesperson: {name}...")
        
        res = await analyze_executive_with_reasoning(name)
        results[name] = res
        print(json.dumps(res, indent=2))
        print("=" * 100 + "\n")
        await asyncio.sleep(1)  # small throttle between requests
    return results
