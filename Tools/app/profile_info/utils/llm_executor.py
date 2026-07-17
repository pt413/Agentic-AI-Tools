import os
import sys
import asyncio
import time
import google.generativeai as genai

# Make sure stdout flushes instantly
sys.stdout.reconfigure(line_buffering=True)

# Configure Gemini API
genai.configure(api_key="AIzaSyBK5gIuhObyv0VgFfkgn8ZdOFu5yLiN5ug")

async def gemini(cust_prompt: str, systemprompt: str = None) -> str:
    
    """Basic Gemini function using gemini-2.0-flash model"""
    try:
        # Use the reliable gemini-2.0-flash model
        model = genai.GenerativeModel('gemini-2.0-flash')
        
        # Create the complete prompt
        full_prompt = f"""
        CONTEXT:{systemprompt}
        
        QUESTION: {cust_prompt}
        
        Provide a clear, professional response based on the data above.
        """
        
        # Generate response
        response = model.generate_content(full_prompt)
        
        # Return the text
        # if response and response.text:
        #     print("ans", response.txt)
        return response.text.strip()
    
            
    except Exception as e:
        print(f"Gemini generation error: {str(e)}")
        return "I'm having trouble processing your request right now. Please try again later."
# Import get_lead from your app
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from generic.external_data import get_lead


def stream_gemini_fast(lead_data: dict, cust_prompt: str):
    start = time.time()
    
    # Use faster model
    model = genai.GenerativeModel("gemini-2.0-flash")
    
    # Extract information from the dictionary (don't convert to string yet!)
    lead_info = lead_data.get('lead_data', {}).get('data', {})
    lead_details = lead_info.get('lead_details', [{}])[0] if lead_info.get('lead_details') else {}
    followups = lead_info.get('lead_followups', [])
    
    # Create a concise prompt
    # concise_prompt = f"""
    # Analyze this lead information:
    
    # Customer: {lead_details.get('name', 'N/A')}
    # Contact: {lead_details.get('contact_details', 'N/A')}
    # Status: {lead_details.get('status', 'N/A')}
    # Location: {lead_details.get('location', 'N/A')}
    # Total Follow-ups: {len(followups)}
    
    # Key recent activities:
    # {[f.get('additional_info', '') for f in followups[:3]]}
    
    # Evaluate salesperson performance for lead ID 250978 based on the call and follow-up history below. Rate on a scale of 1-10 in three categories: Responsiveness Communication Quality Follow-up Effectiveness
    # """
    concise_prompt=f"""
    question {cust_prompt}
    data:{lead_data}"""
    
    print("\n--- Streaming Response (Fast) ---\n")
    
    gemini_prompt = [{"text": concise_prompt}]
    
    try:
        response = model.generate_content(
            gemini_prompt, 
            stream=True,
            generation_config=genai.types.GenerationConfig(
                max_output_tokens=800,  # Limit response length
                temperature=0.2
            )
        )
        
        for chunk in response:
            if chunk.text:
                print(chunk.text, end="", flush=True)
                
    except Exception as e:
        print(f"Error during streaming: {e}")
    
    end = time.time()
    print(f"\n\n--- Completed in {end-start:.2f} seconds ---")

# async def main():
#     # Fetch data asynchronously
#     start = time.time()
#     lead_data = await get_lead("352999")
#     print("Fetched Lead Data Type:", type(lead_data))
    
#     # Check if it's already a dictionary
#     if isinstance(lead_data, dict):
#         print("✅ Data is already a dictionary - no conversion needed")
#         prompt_data = lead_data
#     else:
#         print("⚠️  Data is not a dictionary, but:", type(lead_data))
#         # If it's a string, try to parse it as JSON
#         try:
#             import json
#             prompt_data = json.loads(lead_data)
#             print("✅ Successfully parsed string to dictionary")
#         except:
#             print("❌ Could not parse data, using as-is")
#             prompt_data = lead_data
    
#     cust_prompt = "give me highlight of sales executives task to sales manager in last 24hr"
#     end = time.time()
#     print(f"Data fetched in {end - start:.2f} seconds")
    
#     # Now stream Gemini response - pass the DICTIONARY, not string
#     stream_gemini_fast(prompt_data, cust_prompt)

# if __name__ == "__main__":
#     asyncio.run(main())

# if __name__ == "__main__":
#     asyncio.run(main())

import google.generativeai as genai
import asyncio

async def gemini_with_url(query, url):
    try:
        # Configure Gemini
        genai.configure(api_key="AIzaSyDGZalPuwT13O3bYUe_2xFlvsnHIRSYQHA")

        # Create model with URL context tool
        model = genai.GenerativeModel(
            model_name="gemini-2.5-flash",
            tools=["google_search_retrieval", "url_context"]  # 👈 enables URL reading
        )

      
        response = model.generate_content(
            [
                {"role": "user", "parts": [
                    {"text": query},
                    {"url": url}  
                ]}
            ]
        )

        return response.text.strip()

    except Exception as e:
        return f"Gemini Error: {str(e)}"


# Example usage
# async def main():
#     url = "http://ai.bpai.in/"
#     query = "What is Bright Path doing based on this page?"
#     result = await gemini( url, query)
#     print(result)

# Run async
# asyncio.run(main())


# genai.configure(api_key="AIzaSyAAF1VQ_Qf-kus35soRZK-x-WJq3l9u2nQ")
# async def geminis(prompt: str, cust_prompt: str, systemprompt:str=None) -> str:
#     """Working Gemini function using gemini-pro model"""
#     try:
#         def generate_sync():
#             try:
#                 # Use the reliable gemini-pro model
#                 model = genai.GenerativeModel('gemini-2.0-flash')
                
#                 # Create the complete prompt
#                 full_prompt = f"""
#                 CONTEXT:{systemprompt}
                
#                 QUESTION: {cust_prompt}
                
#                 RELEVANT DATA: {prompt}
                
#                 Provide a clear, professional response based on the data above.
#                 """
                
#                 # Generate response
#                 response = model.generate_content(full_prompt)
                
#                 # Return the text
#                 if response and response.text:
#                     return response.text.strip()
#                 else:
#                     return "I couldn't generate a response. Please try again."
                    
#             except Exception as e:
#                 print(f"Gemini generation error: {str(e)}")
#                 return "I'm having trouble processing your request right now. Please try again later."
        
#         return await asyncio.to_thread(generate_sync)
        
#     except Exception as e:
#         print(f"Gemini outer error: {str(e)}")
#         return "Service temporarily unavailable."
async def geminis(prompt, cust_prompt):
    try:
        # 1. Configure once, not inside the function (see section 3)
        # genai.configure(api_key=f"AIzaSyAAF1VQ_Qf-kus35soRZK-x-WJq3l9u2nQ") # REMOVE THIS
        
        # NOTE: gemini-2.5-flash is already a fast model
        model = genai.GenerativeModel(
            "gemini-2.5-flash",
            system_instruction=f"questions: {cust_prompt}"
        )
        
        # **🔥 KEY CHANGE: Use generate_content_stream for immediate output 🔥**
        response_stream = model.generate_content(prompt, 
        stream=True)
        
        # You will need to process the stream outside this function,
        # perhaps using 'yield' if this were a generator or a framework 
        # that supports async streaming responses.
        full_response = ""
        for chunk in response_stream:
            if chunk.text:
                full_response += chunk.text
                # In a real application (e.g., a web service), you'd 
                # SEND/YIELD chunk.text immediately here.
        
        return full_response.strip()
    except Exception as e:
        return f"Gemini Error: {str(e)}"
