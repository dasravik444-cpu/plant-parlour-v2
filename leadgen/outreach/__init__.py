"""Outreach: e-mail sequences and a WhatsApp send queue, both fed by the Leads tab of the Google Sheet.

The outreach workflow keeps its own state file (outreach.sqlite) and never touches the lead-generation
database: the Google Sheet is the only thing the two automations share.
"""
