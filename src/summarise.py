import os
from openai import OpenAI

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

def summarise_item(title: str, content: str, source: str, section: str, is_update: bool = False) -> str:
    """Return a short, practitioner-focused summary.

    Design goals:
    - Keep it skimmable (few bullets)
    - Immigration relevance first
    - No fluff / no headings like "Key Points:" inside the bullets
    """

    prompt = f"""
You are a UK immigration legal intelligence analyst writing for immigration practitioners.

Source: {source}
Section: {section}
Title: {title}

Content (extract):
{content[:12000]}

Write:

1) Key points (MAX 3 bullets, each under 18 words)
2) Practical takeaways (MAX 2 bullets, each under 18 words)
"""

    if is_update:
        prompt += "\n3) Update note (ONE bullet, under 18 words)\n"

    prompt += """

Rules:
- Be neutral, precise, and conservative.
- If immigration relevance is unclear from the extract, say so briefly in one takeaway.
- If you can identify an effective date / in-force date / stage from the text, include it.
- For cases: state issue + outcome/holding in the key points.
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
