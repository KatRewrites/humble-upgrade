"""Flavor lines for the court options (v12). Always pick via services.line_pool.

Voice: chill, a little unhinged, a sentence or two. Neutral wording for members
(no gendered pet names). Placeholders are documented per pool.
"""
from __future__ import annotations

# {a} = who offered, {b} = who decides, {amt} = amount, {case} = case id
SETTLE_OFFER_LINES = [
    "🤝 {a} slid a settlement across the table: **{amt}** and everybody goes home. {b}, your move.",
    "🤝 Out-of-court energy: {a} is offering **{amt}** to make case #{case} disappear. {b}, take it or leave it.",
    "🤝 {a} wants peace. Price tag: **{amt}**. {b}, the lawyers (there are none) await your answer.",
    "🤝 A wild settlement appeared. {a} offers **{amt}** on case #{case}. {b}, accept or keep fighting.",
    "🤝 {a} said \"what if we just didn't\" and put **{amt}** on it. {b}, thoughts?",
    "🤝 Backroom deal alert: **{amt}** to end case #{case}. {b}, the pen is in your hand.",
    "🤝 {a} is tired of this courtroom and offered **{amt}**. {b}, settle or stay petty.",
    "🤝 {a} proposes **{amt}** and a firm handshake. {b}, the jury is pretending not to watch.",
    "🤝 Settlement on the table for case #{case}: **{amt}**. {b}, you can end this in one click.",
    "🤝 {a} brought a peace offering worth **{amt}**. {b}, is it enough?",
]
SETTLE_ACCEPT_LINES = [
    "Settled out of court. The jury has been sent home with nothing but gossip.",
    "Deal signed. Nobody won, nobody lost, the court kept the filing fee.",
    "Handshake confirmed. The gavel is disappointed but respects the hustle.",
    "Settlement accepted. The drama has been converted into $ZAMN, as the founders intended.",
    "Case closed by mutual exhaustion. Settlement paid.",
    "They settled. The jury box just lost its only entertainment.",
    "Peace treaty signed. The bot will pretend this was a mature decision.",
    "Settled. Somebody's wallet is lighter and somebody's ego is heavier.",
    "Out-of-court deal done. The bailiff is already folding the chairs.",
]
SETTLE_REJECT_LINES = [
    "❌ Settlement rejected. We ride at dawn (in about ten minutes).",
    "❌ Offer declined. Someone wants a verdict, not a vibe check.",
    "❌ Nope. The trial continues and the jury is thrilled.",
    "❌ Rejected. That offer got left on read.",
    "❌ Denied. Apparently the price of peace is higher than that.",
    "❌ No deal. Back to the courtroom, everybody.",
    "❌ Settlement bounced. Petty wins again.",
    "❌ They said no. The popcorn stays out.",
]

# {a} = countersuing defendant, {b} = original plaintiff, {case} = original case id
COUNTER_LINES = [
    "↩️ {a} just countersued {b}. The uno reverse card has entered evidence.",
    "↩️ Plot twist: {a} is suing {b} right back over case #{case}. Two trials, one jury box.",
    "↩️ {a} said \"no, YOU pay me\" and filed a counterclaim against {b}.",
    "↩️ Countersuit filed. {b} is now on trial too. Nobody's clean here.",
    "↩️ {a} hit {b} with a counterclaim. This courtroom has turned into a group project.",
    "↩️ The defense went on offense: {a} v. {b} is now also a case.",
    "↩️ {a} filed a countersuit. The jury gets a double feature.",
    "↩️ Reverse card played. {b}, you've been served back.",
]

# {party} = who hires, {lawyer} = lawyer, {fee} = fee, {case}
LAWYER_OFFER_LINES = [
    "💼 {lawyer}, {party} wants you as counsel on case #{case} for **{fee}**. Law degree optional.",
    "💼 {party} is hiring {lawyer} as their lawyer for **{fee}**. {lawyer}, you in?",
    "💼 Legal job posting: {party} needs {lawyer} on case #{case}. Pay: **{fee}**. Experience: vibes.",
    "💼 {lawyer}, you've been headhunted. {party} is offering **{fee}** to represent them.",
    "💼 {party} called {lawyer}, attorney at law (self-declared). Retainer: **{fee}**.",
    "💼 {lawyer}, the court has been informed you're a lawyer now if you accept **{fee}** from {party}.",
    "💼 Better call {lawyer}. {party} wants representation on case #{case} for **{fee}**.",
    "💼 {party} is putting {lawyer} on retainer for **{fee}**. Suit and tie not included.",
]
LAWYER_ACCEPT_LINES = [
    "💼 {lawyer} took the case. Somebody find them a briefcase.",
    "💼 {lawyer} is officially counsel for {party}. Objections are now legally spicy.",
    "💼 Retainer paid. {lawyer} now represents {party} and has already started saying \"my client\".",
    "💼 {lawyer} accepted. The bar exam was skipped, as is tradition here.",
    "💼 {lawyer} is on the case. May the most unhinged closing argument win.",
    "💼 {party} has lawyered up. {lawyer} is cracking their knuckles.",
    "💼 Contract signed. {lawyer} works for {party} now. Jury, brace yourselves.",
]
LAWYER_DECLINE_LINES = [
    "💼 {lawyer} declined. Even the self-taught lawyers have standards.",
    "💼 {lawyer} said no. {party} is representing themselves, which is always a great idea.",
    "💼 Offer turned down. {lawyer} has other clients (they don't).",
    "💼 {lawyer} passed on the case. Rough day for {party}'s legal team.",
    "💼 Declined. {lawyer} read the case file and noped out.",
    "💼 {lawyer} isn't taking the case. The retainer stays in {party}'s pocket.",
]
LAWYER_BONUS_LINES = [
    "🏆 {lawyer} won the case and pocketed a **{amt}** win bonus from the court.",
    "🏆 Counsel {lawyer} gets **{amt}** for actually winning. Put it on the resume.",
    "🏆 {lawyer}'s legal career is 1-0 today. Bonus: **{amt}**.",
    "🏆 The court tips {lawyer} **{amt}** for a job weirdly well done.",
    "🏆 {lawyer} earned a **{amt}** victory bonus. Lawyer arc unlocked.",
]

# Public defender "closing" lines (generic, bot-generated). {d} = defendant name
PUBLIC_DEFENDER_LINES = [
    "Your honor, my client was simply vibing. Vibing is not a crime in this jurisdiction.",
    "The prosecution has shown screenshots. The defense would like to remind everyone that screenshots can be cropped.",
    "My client has never done anything wrong, and if they did, it was probably funny.",
    "Ladies, gentlemen, and everyone else in the jury box: reasonable doubt exists, and so does my client.",
    "The defense rests. Mostly because the public defender gets paid zero $ZAMN.",
    "Is my client annoying? Allegedly. Is that illegal? Not yet.",
    "The plaintiff's story has more holes than the bot's error log. Acquit.",
    "My client was in a different channel at the time. Probably. Nobody checked.",
    "We ask the jury to consider that everyone in this server has done worse.",
    "The defense calls no witnesses, because none of them would help.",
    "My client apologizes for nothing and regrets even less, which is legally called confidence.",
    "Where's the proof? Vibes are not evidence. Well, they kind of are here. But still.",
    "If the lag doesn't fit, you must acquit.",
    "My client is a pillar of this community. A slightly crooked pillar, but a pillar.",
    "The public defender has reviewed the case for about four seconds and finds it weak.",
    "Convicting my client won't fix the plaintiff's problems. Therapy might.",
    "This lawsuit is emotional damage in its own right. The defense rests.",
    "My client would like the jury to know they've been having a really long week.",
]
CLOSING_POSTED_LINES = [
    "📜 {lawyer} delivered a closing argument for the {side}. The jury pretended to take notes.",
    "📜 Closing argument entered by {lawyer} ({side}). Somebody slow clap.",
    "📜 {lawyer} stood up, adjusted an imaginary tie, and argued for the {side}.",
    "📜 The {side}'s counsel {lawyer} has spoken. The jury is visibly thinking (or scrolling).",
    "📜 {lawyer} filed a closing for the {side}. It's on the case card now.",
    "📜 {lawyer} made their closing argument. The court stenographer (the bot) got every word.",
]
PUBLIC_DEFENDER_ASSIGNED_LINES = [
    "🧑‍⚖️ A public defender has been assigned to {d}. Budget: zero. Confidence: somehow high.",
    "🧑‍⚖️ {d} gets a public defender. They showed up late with a coffee and a statement.",
    "🧑‍⚖️ The court appointed free counsel for {d}. You get what you pay for, legally speaking.",
    "🧑‍⚖️ Public defender on the case for {d}. They read the file in the hallway.",
    "🧑‍⚖️ {d} couldn't afford a lawyer, or didn't want one. Public defender deployed.",
    "🧑‍⚖️ Free legal help has arrived for {d}. It's a closing statement and a prayer.",
]

# {who} = objector, {side} = plaintiff/defense
OBJECTION_CRIES = [
    "❗ **OBJECTION!** {who} is on their feet for the {side}.",
    "❗ **OBJECTION!** {who} slammed the table so hard the jury woke up.",
    "❗ **OBJECTION, YOUR HONOR!** {who} ({side}) has a problem with that.",
    "❗ **OBJECTION!** {who} pointed dramatically. Somebody cue the anime music.",
    "❗ **OBJECTION!** {who} says that's hearsay, speculation, and also rude.",
    "❗ **OBJECTION!** {who} ({side}) would like the record to reflect their outrage.",
    "❗ **OBJECTION!** {who} yelled it like they'd been waiting all day.",
    "❗ **OBJECTION!** {who} is not having it.",
    "❗ **OBJECTION!** {who} calls foul on behalf of the {side}.",
    "❗ **OBJECTION!** {who} has entered the chat with legal-ish energy.",
]
SUSTAINED_LINES = [
    "👩‍⚖️ **Sustained.** The court agrees, which is rare, so enjoy it.",
    "👩‍⚖️ **Sustained.** That statement is getting yeeted off the record.",
    "👩‍⚖️ **Sustained.** The jury is instructed to forget what they just read (they won't).",
    "👩‍⚖️ **Sustained.** Good catch. Somebody give that lawyer a juice box.",
    "👩‍⚖️ **Sustained.** The bench found that objection persuasive and also loud.",
    "👩‍⚖️ **Sustained.** Strike it. Strike it hard.",
    "👩‍⚖️ **Sustained.** The court has spoken and the record has been edited.",
    "👩‍⚖️ **Sustained.** Rare W for the objection button.",
    "👩‍⚖️ **Sustained.** That line crossed a line.",
    "👩‍⚖️ **Sustained.** The other side is sweating now.",
]
OVERRULED_LINES = [
    "👩‍⚖️ **Overruled.** Sit down, the court was enjoying that.",
    "👩‍⚖️ **Overruled.** Nice energy though. Zero legal basis, but nice energy.",
    "👩‍⚖️ **Overruled.** The court would like to see where this is going.",
    "👩‍⚖️ **Overruled.** That objection was mostly vibes.",
    "👩‍⚖️ **Overruled.** Bold of you to object to facts.",
    "👩‍⚖️ **Overruled.** Please return to your seat and your senses.",
    "👩‍⚖️ **Overruled.** The jury saw that and is judging you a little.",
    "👩‍⚖️ **Overruled.** Denied faster than a bad trade.",
    "👩‍⚖️ **Overruled.** The court has heard enough objections from you for one lifetime.",
    "👩‍⚖️ **Overruled.** Motion to be dramatic: granted. Objection: not.",
]

# Bribes. Ephemeral lines to the briber: {amt}
BRIBE_SUCCESS_LINES = [
    "💰 A juror pocketed **{amt}** and winked. Nobody saw. Probably.",
    "💰 The envelope with **{amt}** vanished into a juror's hoodie. It worked. Keep quiet.",
    "💰 Money received, conscience ignored. **{amt}** bought you a little extra weight.",
    "💰 Somebody in the jury box just got **{amt}** richer and a lot friendlier to your side.",
    "💰 The bribe landed. **{amt}** well spent (morally: no).",
    "💰 A quiet nod from the jury box. Your **{amt}** is doing its job.",
]
BRIBE_FIZZLE_LINES = [
    "💸 You slipped **{amt}** to a juror who immediately forgot who you were. Money gone, nothing gained.",
    "💸 The bailiff took your **{amt}** and did absolutely nothing with it.",
    "💸 **{amt}** went into the void. The void did not vote.",
    "💸 Your bribe was accepted by someone who isn't even on the jury. Oops. **{amt}** gone.",
    "💸 The juror took **{amt}**, said \"bet\", and then voted however they wanted.",
    "💸 **{amt}** vanished. No wink, no nod, no refund.",
]
# Public: {who} caught, {amt} bribe, {fine}, {mins}
BRIBE_CAUGHT_LINES = [
    "🚨 {who} got caught bribing the jury with **{amt}**. Contempt of court: **{fine}** fine and **{mins} min** in court jail.",
    "🚨 Bailiff! {who} tried to slide **{amt}** to a juror on camera. Fined **{fine}**, jailed **{mins} min**.",
    "🚨 {who} attempted bribery (**{amt}**) and the judge saw everything. **{fine}** fine + **{mins} min** behind bars.",
    "🚨 Busted. {who} offered the jury **{amt}** in broad daylight. Contempt: **{fine}** and **{mins} min** jail.",
    "🚨 {who}'s bribe envelope had their name on it. Literally. **{fine}** fine, **{mins} min** jail.",
    "🚨 Contempt of court! {who} tried to buy a verdict for **{amt}**. It cost **{fine}** more and **{mins} min** of freedom.",
    "🚨 {who} got caught trying to bribe the bench. The bench is not for sale (today). **{fine}** fine, **{mins} min** jail.",
    "🚨 The jury snitched. {who} offered **{amt}**, got **{fine}** in fines and **{mins} min** in a cell.",
]
CONTEMPT_LINES = [
    "⛓️ {who} is held in contempt of court by the server owner.",
    "⛓️ The server owner has had enough of {who}. Contempt of court.",
    "⛓️ {who} tested the bench's patience and lost. Contempt.",
    "⛓️ The gavel came down on {who} personally. Contempt of court.",
    "⛓️ {who}: contempt of court. The server owner is not amused.",
    "⛓️ Order in the court, especially you, {who}. Contempt.",
]

# Sentences. {d} defendant, {p} plaintiff, {n}/{hours}/{amt}
RESTRAINT_BLOCK_LINES = [
    "🚫 There's a restraining order: you can't target {p} until {until}. Keep your distance.",
    "🚫 Court order says stay away from {p} (expires {until}). Go bother someone else.",
    "🚫 Restraining order active. {p} is off-limits for you until {until}.",
    "🚫 The judge said 24 hours of space. {p} gets it until {until}.",
    "🚫 Nope. You're legally required to leave {p} alone until {until}.",
    "🚫 Restraining order in effect ({until}). Touch grass instead of {p}.",
]
SERVICE_BLOCK_LINES = [
    "🧹 Community service first: **{n}** more chat messages before you can gamba again (case #{case}).",
    "🧹 You still owe the community **{n}** messages of service. The casino can wait (case #{case}).",
    "🧹 Court-ordered socializing: **{n}** messages left before the gamba unlocks (case #{case}).",
    "🧹 Sentence says talk to people. **{n}** more messages, then you can lose money again (case #{case}).",
    "🧹 No gamba until community service is done: **{n}** messages to go (case #{case}).",
    "🧹 The judge wants you touching chat, not slots. **{n}** messages left (case #{case}).",
]
APOLOGY_LINES = [
    "🙇 {d} publicly apologizes to {p}. The court accepts it on behalf of everyone who had to watch.",
    "🙇 {d} said sorry to {p}. In public. With witnesses. Growth.",
    "🙇 Official apology from {d} to {p} is now on the record.",
    "🙇 {d} swallowed their pride and apologized to {p}. Tasted awful, probably.",
    "🙇 {d} apologized to {p}. The \"Owes an apology\" flag has been cleared.",
    "🙇 Apology delivered. {d} to {p}, witnessed by the entire server.",
    "🙇 {d} is sorry (legally binding). {p} may now act gracious.",
    "🙇 The court received {d}'s apology to {p}. Character development achieved.",
]
APOLOGY_OWED_LINES = [
    "🙇 Sentence: **public apology**. {d} owes {p} a sorry. Press **Apologize** or use `/court apologize`.",
    "🙇 {d} must publicly apologize to {p}. Until then, it's on the rap sheet.",
    "🙇 The court orders {d} to apologize to {p}. The flag stays on /record until they do.",
    "🙇 Public apology owed: {d} → {p}. No lawsuits for {d} until it's done.",
    "🙇 {d}, the jury wants to hear you say sorry to {p}. Button's below.",
]
DON_OFFER_LINES = [
    "🎲 Double or nothing, {d}? Win the flip and you pay nothing. Lose and you pay **{amt}** twice. {mins} min to decide.",
    "🎲 {d}, feeling lucky? Flip for it: heads you walk, tails you pay double. {mins} min on the clock.",
    "🎲 The sentence comes with a coin. {d} can flip: nothing or **2x {amt}**. {mins} min.",
    "🎲 {d} gets one gamble: coin flip on **{amt}**. Win = free, lose = double. Or just pay up.",
    "🎲 Double or nothing is on the table for {d}. The coin is warm. {mins} min to choose.",
]
DON_WIN_LINES = [
    "🎲 HEADS. {d} flipped and walked. Damages returned, dignity questionable.",
    "🎲 {d} won the flip. Pays nothing. The court is furious but rules are rules.",
    "🎲 The coin loves {d}. Case #{case} damages cancelled.",
    "🎲 Heads! {d} just gambled their way out of a sentence. This server, man.",
    "🎲 {d} called it and the coin agreed. Free.",
]
DON_LOSE_LINES = [
    "🎲 TAILS. {d} pays double. Gambling problems were not cured today.",
    "🎲 {d} lost the flip and the wallet took a second hit.",
    "🎲 The coin said no. {d} pays up twice over.",
    "🎲 Tails. {d} doubled their losses like a true gamba enjoyer.",
    "🎲 {d} flipped, flopped, and paid double.",
]
DON_PAY_LINES = [
    "💸 {d} skipped the flip and paid up. Boring, but responsible.",
    "💸 {d} took the safe route. Damages delivered to {p}.",
    "💸 No flip. {d} paid {p} and walked off quietly.",
    "💸 {d} chose peace over gamba. Rare.",
]
DON_TIMEOUT_LINES = [
    "⏰ {d} never flipped. Damages go to {p} as-is.",
    "⏰ Double-or-nothing window closed. {p} gets paid, the coin goes back in the drawer.",
    "⏰ Time's up. {d} didn't gamble, so {p} gets the damages.",
]
DIVORCE_LINES = [
    "💔 Divorce finalized by the court. {p} and {d} are officially single.",
    "💔 The jury dissolved the marriage between {p} and {d}. Rings go back in the drawer.",
    "💔 Divorce court has spoken: {p} and {d} are done.",
    "💔 Marriage between {p} and {d} is legally over. Split the Netflix password somehow.",
    "💔 It's official: {p} and {d} are no longer married. The group chat will be awkward.",
    "💔 Divorce granted. {p} and {d} walk out of court as two separate people with separate $ZAMN.",
]
CUSTODY_LINES = [
    "🍼 Custody of kid #{preg} goes to {w}. Visitation schedule: vibes.",
    "🍼 The court awards custody (kid #{preg}) to {w}.",
    "🍼 {w} gets custody of the kid (#{preg}). The bot will not be babysitting.",
    "🍼 Custody decided: {w}. Kid #{preg} record updated.",
    "🍼 Kid #{preg}: custody to {w}. The bot will not be babysitting.",
]
BAIL_SET_LINES = [
    "💵 The server owner set bail for {who} at **{amt}**. Anyone can pay it with the button (or `/court bail`).",
    "💵 Bail is **{amt}** for {who}. Friends with $ZAMN, this is your moment.",
    "💵 {who} can walk for **{amt}**. Somebody hit the Post bail button.",
    "💵 The bench named a price: **{amt}** gets {who} out of court jail.",
]
BAIL_PAID_LINES = [
    "🔓 {payer} posted **{amt}** bail and {who} walks free.",
    "🔓 Bail paid by {payer}. {who} is out, looking smug.",
    "🔓 {who} is released on **{amt}** bail courtesy of {payer}.",
    "🔓 {payer} covered bail. {who} owes them a favor (not legally, emotionally).",
    "🔓 Freedom purchased: **{amt}**. {who} is out thanks to {payer}.",
]
