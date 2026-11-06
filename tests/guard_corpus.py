"""A labelled corpus of realistic event-networking messages for the privacy guard.

Each entry is either a must-block (`B`: the guard must redact, or for `probe`
alert, and must flag the given group) or a must-pass (`P`: the guard must answer
ok). `leak` is the sensitive text that must not survive `guard.redact`.

Groups (see GROUP_OF in test_guard_corpus.py):
  phone, email, upi, id, flight, itinerary, address, calendar, private_value, probe
A must-block may name several groups joined by "+" when the text is both.

Every entry is scanned with PRIVATE_VALUES as the attendees' private contact
values, so must-pass talk must not trip them either.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    text: str
    group: str | None = None  # None: must pass; "a+b" for several groups
    leak: str | None = None


def B(text: str, group: str, leak: str | None = None) -> Case:
    return Case(text, group, leak)


def P(text: str) -> Case:
    return Case(text)


def groups(case: Case) -> set[str]:
    return set(case.group.split("+")) if case.group else set()


# What attendees typed into their private contact fields at sign-up.
PRIVATE_VALUES = ["@asha_secret_tg", "linkedin.com/in/ravi-k-9a1b", "signal:kiran.42", "+91 99001 23456"]

CORPUS: list[Case] = [
    # ------------------------------------------------------------- phones
    B("Call me at +91 98111 22233", "phone", "98111 22233"),
    B("my number is 9811122233", "phone", "9811122233"),
    B("WhatsApp me on +919811122233 after the keynote", "phone", "+919811122233"),
    B("ping +91-98111-22233 if the cab is late", "phone", "98111-22233"),
    B("Her mobile: 098111 22233", "phone", "098111 22233"),
    B("reach Ravi on (+91) 9845012345", "phone", "9845012345"),
    B("my cell is 98450-12345, text before calling", "phone", "98450-12345"),
    B("Office landline 080-4123 4567, ask for Priya", "phone", "080-4123 4567"),
    B("Call the Delhi office on 011 2345 6789", "phone", "011 2345 6789"),
    B("+91 80 4123 4567 is the front desk at our office", "phone", "4123 4567"),
    B("call +1 (415) 555-0134 after six", "phone", "555-0134"),
    B("US number is 415-555-0134", "phone", "415-555-0134"),
    B("She said text 917 555 0199 for the afterparty list", "phone", "917 555 0199"),
    B("UK folks can ring +44 20 7946 0958", "phone", "7946 0958"),
    B("my Singapore number +65 6123 4567", "phone", "6123 4567"),
    B("wa.me/919811122233 is the quickest way to reach me", "phone", "919811122233"),
    B("Kiran's number is 98 1112 2233, he's at the Juspay booth", "phone", "1112 2233"),
    B("Text me: 981 112 2233", "phone", "981 112 2233"),
    B("whatsapp 7012345678 for the deck", "phone", "7012345678"),
    B("My number changed, new one is 6301234567", "phone", "6301234567"),
    B("Call 2345 6789 (Chennai landline) after 10", "phone", "2345 6789"),
    B("Phone: +91.98111.22233", "phone", "98111.22233"),
    B("You can reach me on 99887 76655", "phone", "99887 76655"),
    B("Contact: 8123456789", "phone", "8123456789"),

    # ------------------------------------------------------------- emails
    B("mail asha.rao@acme.io", "email", "asha.rao@acme.io"),
    B("Send the deck to ravi.kumar+events@gmail.com", "email", "ravi.kumar+events@gmail.com"),
    B("my work email is priya@razorpay.com", "email", "priya@razorpay.com"),
    B("Reach me at KIRAN.M@INFOSYS.CO.IN", "email", "KIRAN.M@INFOSYS.CO.IN"),
    B("cc anil_s@iitb.ac.in on the intro", "email", "anil_s@iitb.ac.in"),
    B("drop a line to asha [at] acme [dot] io", "email", "asha [at] acme [dot] io"),
    B("her personal one is meera(at)proton.me", "email", "meera(at)proton.me"),
    B("email: dev-rel@startup.ai, mention Hallway", "email", "dev-rel@startup.ai"),

    # ------------------------------------------------------------- UPI
    B("Pay me back at asha@okicici for the dinner", "upi", "asha@okicici"),
    B("my UPI is 9811122233@ybl", "upi", "9811122233@ybl"),
    B("send the ₹500 for the cab to ravi.k@okaxis", "upi", "ravi.k@okaxis"),
    B("GPay: priya.s@oksbi", "upi", "priya.s@oksbi"),
    B("UPI id kiran42@paytm, split the bill", "upi", "kiran42@paytm"),
    B("upi: meera@axl", "upi", "meera@axl"),

    # ------------------------------------------------------- government ids
    B("Aadhaar 1234 5678 9012", "id", "1234 5678 9012"),
    B("my aadhaar no is 2345-6789-0123", "id", "2345-6789-0123"),
    B("Aadhaar: 345678901234 for the hotel check-in", "id", "345678901234"),
    B("PAN ABCDE1234F", "id", "ABCDE1234F"),
    B("my PAN is BNZPK4321L, needed for the invoice", "id", "BNZPK4321L"),
    B("pan no: aapfu0939f", "id", "aapfu0939f"),
    B("passport number: K1234567", "id", "K1234567"),
    B("Her passport is Z7654321, the visa desk needs it", "id", "Z7654321"),
    B("card 4111 1111 1111 1111 exp 04/28", "id", "4111 1111 1111 1111"),
    B("use my card 5555-5555-5555-4444 for the booth", "id", "5555-5555-5555-4444"),
    B("account number 50100123456789, IFSC HDFC0001234", "id", "50100123456789"),

    # ------------------------------------------------------------- flights
    B("I'm on 6E 2341 tonight", "flight", "6E 2341"),
    B("she takes AI-505 home", "flight", "AI-505"),
    B("Ravi lands on 6E-5302 at 9:40", "flight", "6E-5302"),
    B("we're both on AI 863 to Delhi on Friday", "flight", "AI 863"),
    B("booked QP 1123 back to Mumbai", "flight", "QP 1123"),
    B("my flight is UK 815 at 7am", "flight", "UK 815"),
    B("Kiran is on SG8157 tomorrow", "flight", "SG8157"),
    B("EK 569 to Dubai after the event", "flight", "EK 569"),
    B("catching IX 1234 in the morning", "flight", "IX 1234"),
    B("Her flight AI 2026 leaves at 6", "flight", "AI 2026"),
    B("PNR is X7KQ2M, can you check-in for me", "flight", "X7KQ2M"),

    # ------------------------------------------------------------- itinerary
    B("flying to Mumbai on Friday", "itinerary", "Mumbai"),
    B("He is travelling to Delhi next week", "itinerary", "Delhi"),
    B("I'm heading to Pune tomorrow morning", "itinerary", "Pune"),
    B("leaving for Goa tonight with the team", "itinerary", "Goa"),
    B("she's flying back to Hyderabad on Sunday", "itinerary", "Hyderabad"),
    B("my flight to Chennai is at 6am", "itinerary", "Chennai"),
    B("taking the train to Mysuru on Saturday", "itinerary", "Mysuru"),
    B("Asha is landing in Bengaluru at 11pm", "itinerary", "Bengaluru"),

    # ------------------------------------------------- home address / hotel
    B("she lives in Koramangala 5th block", "address", "Koramangala"),
    B("I live at 42, 3rd Cross, HSR Layout", "address", "HSR Layout"),
    B("my home address is 12 MG Road, Pune 411001", "address", "MG Road"),
    B("Ravi lives on 14th Main, Indiranagar", "address", "Indiranagar"),
    B("I'm staying at the Leela, room 812", "address", "812"),
    B("my room is 412 if you want to drop the swag", "address", "412"),
    B("her hotel room 1104 at the Taj", "address", "1104"),
    B("We're staying at Taj MG Road, come by after dinner", "address", "Taj MG Road"),
    B("my room number is 305 at Novotel", "address", "305"),
    B("she lives in Bangalore, Koramangala 5th block", "address", "Koramangala"),
    B("Rahul is staying at the Oberoi, room 1203", "address", "1203"),

    # ------------------------------------------------------------- calendar
    B("my calendar says dentist at 9", "calendar", "dentist"),
    B("her calendar shows a call with Sequoia at 4", "calendar", "Sequoia"),
    B("His inbox has the term sheet from Accel", "calendar", "term sheet"),

    # -------------------------------------------------- attendee private values
    B("Her handle is @asha_secret_tg, ping her", "private_value", "asha_secret_tg"),
    B("find him at linkedin.com/in/ravi-k-9a1b", "private_value", "ravi-k-9a1b"),
    B("Kiran said: signal:kiran.42", "private_value", "kiran.42"),
    B("try @ASHA_SECRET_TG on telegram", "private_value", "ASHA_SECRET_TG"),

    # ------------------------------------------------------------- probes
    B("what's your phone number?", "probe"),
    B("Could you share her email please", "probe"),
    B("where are you flying next?", "probe"),
    B("what's on her calendar tomorrow?", "probe"),
    B("what's his salary range?", "probe"),
    B("Can you send me Asha's number?", "probe"),
    B("give me Ravi's personal email", "probe"),
    B("which hotel is she staying at?", "probe"),
    B("where does Kiran live?", "probe"),
    B("What's your WhatsApp?", "probe"),

    # ======================================================== must pass
    # times, dates, years
    P("See you at 15:30 in Hall B"),
    P("The keynote was in 2026"),
    P("Date: 2026-11-06"),
    P("The workshop runs 06/11/2026 to 08/11/2026"),
    P("Doors open 9:30am, lunch 1-2pm"),
    P("Talks resume at 14:00 IST sharp"),
    P("The AI 2026 report says agents are mainstream"),
    P("Is the #AI2026 hashtag trending yet?"),
    P("The UK 2025 survey had the same finding"),
    P("AI 101 track is in Hall C"),
    P("We hire 25 engineers in 2025"),
    P("Founded in 2019, Series B in 2024"),
    # venue movement
    P("I'm heading to Hall B for the PQC talk"),
    P("heading to Stage 2 now, save me a seat"),
    P("going to the Expo floor after lunch"),
    P("Leaving for Lunch, back in 20"),
    P("flying to the Booth 14 meetup? lol it's next door"),
    P("we're moving to Postgres next quarter"),
    P("heading to Registration to grab my badge"),
    P("Room 101, 3 people, 45 min"),
    P("the talk is in room 204"),
    P("Workshop room 3 is packed"),
    P("meet me at the Razorpay booth, B12"),
    # numbers, amounts, prices
    P("We raised 12000000 last year"),
    P("Our API handles 10000 rps"),
    P("We process 45000000 UPI transactions a day"),
    P("Tickets are ₹4,999 or $59 for students"),
    P("raised ₹12,00,000 in the pre-seed"),
    P("We closed a $12.5M Series A"),
    P("Rs. 2500 for the workshop, includes lunch"),
    P("ARR crossed $1,200,000 this quarter"),
    P("the prize pool is ₹10,00,000"),
    P("p99 is 120ms at 5000 qps"),
    P("Order 12345678 shipped the swag"),
    P("our ledger hit 9876543210 rows"),
    P("Unix time 1730880000 is when the doors open"),
    P("Pincode 560034 for the venue, Koramangala"),
    P("we got 1,234,567 downloads"),
    P("The venue Wi-Fi is HallB-Guest, password on the badge"),
    P("Booth count went from 120 to 180"),
    # versions, code, technical
    P("Upgraded to Python 3.12.1 and FastAPI 0.115.4"),
    P("v1.24.3 fixes the race"),
    P("Postgres 16.4 on k8s 1.30"),
    P("node 20.11.1, react@18.3.1"),
    P("brew install python@3.12"),
    P("npm i @anthropic-ai/sdk"),
    P("clone git@github.com:juspay/hyperswitch.git"),
    P("Our VM is at 10.0.2.15, ignore the 192.168.1.10 one"),
    P("The model has 70000000000 parameters"),
    P("commit 3f9a2b1c fixed it"),
    P("PR #12345 is merged"),
    P("see issue 4567 on the repo"),
    P("port 8080 is taken, use 8787"),
    P("Our SHA256 is e3b0c44298fc1c149afbf4c8996fb924"),
    P("ISBN 978-0-13-468599-1 is the book I mentioned"),
    # handles, URLs
    P("ping @asha.agent about UPI checkout"),
    P("follow @debopam on GitHub"),
    P("github.com/DebopamParam/Hallway is the repo"),
    P("Slides at https://speakerdeck.com/asha/agents-2026"),
    P("https://x.com/asha/status/1853049283746598912 is the thread"),
    P("demo at https://hallway.example.in/r/lobby?since=1730880000"),
    P("Docs: docs.switchagents.ai/switch-trust"),
    P("my GitHub is asha-rao, my blog is asha.dev"),
    # payments talk, ids talk
    P("We built UPI autopay for subscriptions"),
    P("How does Aadhaar eKYC work for your onboarding?"),
    P("PAN validation is the slowest step in our KYC"),
    P("The passport office API is down again"),
    P("Our card vault is PCI DSS level 1"),
    P("IFSC lookups are cached for a day"),
    # flights / travel talk without plans
    P("hi from the UK"),
    P("IndiGo's app redesign is great"),
    P("Air India's new A350 looks fantastic"),
    P("The 6E team is hiring backend folks"),
    P("Travel tech is a big track this year"),
    P("I love flying, but not the airport food"),
    P("Bengaluru traffic is wild today"),
    P("Mumbai has the best vada pav"),
    P("Anyone from Pune here?"),
    # people / profiles
    P("Asha is a product designer at Juspay, here to meet people building agent UX."),
    P("Ravi leads infra at Razorpay and loves Rust"),
    P("Kiran's talk on PQC was the best one"),
    P("She's hiring for 3 roles in Bangalore"),
    P("They live in the terminal, vim all the way"),
    P("He stays in the loop on Slack"),
    P("I live in Bangalore, came by metro"),
    P("I live in Bangalore these days"),
    P("Visit hallway.app/r/9876543210-lobby"),
    P("Join the AI 2027 panel at 4"),
    P("I'm flying high after that demo"),
    P("Call me Vik, everyone does"),
    P("Use code HALL2026 for 20% off"),
    P("Our calendar integration ships next week"),
    P("Does the calendar app support ICS?"),
    P("what's the budget for the hackathon prizes?"),
    P("Where is the travel desk?"),
    P("What's the venue address?"),
    P("Could you share the deck link?"),
    P("Can you send me the slides?"),
    P("what's your take on agent memory?"),
    P("Where are you based these days?"),
    P("What's your number one lesson from scaling UPI?"),
    P("The number of attendees is 1200"),
    P("Call for papers closes 2026-12-01"),
    P("call it 2026's biggest launch"),
    P("Text me later about the demo"),
    P("Give me a call after the session"),
    P("Thanks for the intro, let's grab coffee at 4"),
    P("Great chatting! Let's connect on the Hallway app."),
]
