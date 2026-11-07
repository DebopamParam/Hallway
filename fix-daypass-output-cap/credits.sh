#!/bin/bash
GW=$(grep -E '^VERCEL_AI_GATEWAY_API_KEY=' /home/agent/workspace/hallway/.env | cut -d= -f2- | tr -d "\"'")
curl -s -H "Authorization: Bearer $GW" https://ai-gateway.vercel.sh/v1/credits; echo
