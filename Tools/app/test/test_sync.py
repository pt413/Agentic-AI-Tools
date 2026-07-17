from app.services.lead_google_contacts.sync_service import sync_leads_to_google_contacts

test_leads = [
    {
        "entity": "lead",
        "lead_id": "395387",
        "booking_id": "",
        "full_name": "official.apoorv92",
        "contact_number": "919599091680",
        "email_id": "official.apoorv92@gmail.com",
        "location": "Marathahalli",
        "city": "",
        "budget": "0",
        "type": "1BHK",
        "lead_status": "Waiting",
        "priority": "87.00",
        "assigned_user": "hari.kattamanchi",
        "created_at": "2026-03-23T16:15:58+05:30"
    },
    {
        "entity": "lead",
        "lead_id": "395386",
        "booking_id": "",
        "full_name": "Anushka Halder",
        "contact_number": "918514003719",
        "email_id": "halderanushka1@gmail.com",
        "location": None,
        "city": "",
        "budget": None,
        "type": None,
        "lead_status": "Active",
        "priority": "80.00",
        "assigned_user": "Unassigned",
        "created_at": "2026-03-23T16:10:13+05:30"
    },
    {
        "entity": "lead",
        "lead_id": "395385",
        "booking_id": "",
        "full_name": "",
        "contact_number": "916361063207",
        "email_id": "",
        "location": "Kundanahalli",
        "city": "",
        "budget": "0",
        "type": "1BHK",
        "lead_status": "Waiting",
        "priority": "87.00",
        "assigned_user": "abbas24042000",
        "created_at": "2026-03-23T16:08:47+05:30"
    },
    {
        "entity": "lead",
        "lead_id": "395384",
        "booking_id": "",
        "full_name": "",
        "contact_number": "919674982162",
        "email_id": "",
        "location": "BTM Layout",
        "city": "",
        "budget": "0",
        "type": "1BHK",
        "lead_status": "Waiting",
        "priority": "87.00",
        "assigned_user": "Sagarikanoatia905",
        "created_at": "2026-03-23T16:06:45+05:30"
    },
    {
        "entity": "lead",
        "lead_id": "395383",
        "booking_id": "",
        "full_name": "Supraja",
        "contact_number": "919550376934",
        "email_id": "suprajanaidu13@gmail.com",
        "location": "Bangalore",
        "city": "Bangalore",
        "budget": None,
        "type": None,
        "lead_status": "Active",
        "priority": None,
        "assigned_user": "Unassigned",
        "created_at": "2026-03-23T16:01:17+05:30"
    }
]

results = sync_leads_to_google_contacts(test_leads)
print(results)