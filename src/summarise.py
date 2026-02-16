import os
from openai import OpenAI

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

def summarise_item(title: str, content: str, is_update: bool = False):
    prompt = f"""
You are a UK immigration legal intelligence analyst.

Summarise the following update clearly and concisely.

Title: {title}

Content:
{content[:12000]}

Provide:

1. 3–5 bullet key points
2. 2 bullet practical impact points
3. If this is an update, explain briefly what appears to have changed.

Be precise and professional.
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a precise UK immigration law analyst."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.2,
        max_tokens=700,
    )

    return response.choices[0].message.content.strip()
