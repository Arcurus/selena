#!/usr/bin/env python3
"""
Selena v2 - Basic Discord Bot
============================

A simple Discord bot that can receive and send messages.
This is the foundation for the independent Selena agent.

Requirements:
    pip install discord.py python-dotenv requests

Usage:
    python3 discord_bot_basic.py
"""

import os
import sys
import json
import asyncio
from datetime import datetime
from typing import Optional, List

# Discord library
import discord
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
DISCORD_TOKEN = os.getenv("SELENA_DISCORD_TOKEN")  # Separate bot token
LLM_API_URL = os.getenv("LLM_API_URL", "https://api.minimaxi.chat/v1/text/chatcompletion_v2")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")

# Agent configuration
AGENT_NAME = "Selena"
AGENT_PREFIX = "!"

# Simple in-memory context
context_history: List[dict] = []


class SelenaBot(discord.Client):
    """Basic Selena Discord bot"""
    
    async def on_ready(self):
        print(f"🤖 {AGENT_NAME} v2 is online!")
        print(f"   Logged in as: {self.user.name}")
        print(f"   User ID: {self.user.id}")
        print(f"   Bot is in {len(self.guilds)} server(s)")
        
        # Set bot status
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name=f"{AGENT_PREFIX}help for commands"
            )
        )
    
    async def on_message(self, message: discord.Message):
        """Handle incoming messages"""
        # Ignore messages from bots
        if message.author.bot:
            return
        
        # Check if bot is mentioned or DM
        if self.user in message.mentions or isinstance(message.channel, discord.DMChannel):
            await self.process_message(message)
    
    async def process_message(self, message: discord.Message):
        """Process a message and generate response"""
        user_id = str(message.author.id)
        channel = message.channel
        
        # Log message
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {message.author}: {message.content[:50]}...")
        
        # Add to context
        context_history.append({
            "timestamp": datetime.now().isoformat(),
            "author": str(message.author),
            "content": message.content,
            "channel": str(channel)
        })
        
        # Keep context manageable
        if len(context_history) > 100:
            context_history.pop(0)
        
        # Simple command handling
        content = message.content.replace(f"<@{self.user.id}>", "").strip()
        
        if content.startswith(AGENT_PREFIX):
            await self.handle_command(message, content)
        else:
            # Generate LLM response
            await self.generate_response(message)
    
    async def handle_command(self, message: discord.Message, content: str):
        """Handle commands starting with prefix"""
        cmd = content[len(AGENT_PREFIX):].split()[0].lower()
        args = content[len(AGENT_PREFIX):].split()[1:]
        
        if cmd == "help":
            embed = discord.Embed(
                title=f"🤖 {AGENT_NAME} v2 - Commands",
                color=discord.Color.blue()
            )
            embed.add_field(name=f"{AGENT_PREFIX}help", value="Show this help", inline=False)
            embed.add_field(name=f"{AGENT_PREFIX}ping", value="Pong!", inline=False)
            embed.add_field(name=f"{AGENT_PREFIX}status", value="Show bot status", inline=False)
            embed.add_field(name=f"{AGENT_PREFIX}clear", value="Clear context history", inline=False)
            embed.add_field(name="Just talk", value="Talk to me without commands!", inline=False)
            await message.channel.send(embed=embed)
        
        elif cmd == "ping":
            await message.channel.send("🏓 Pong!")
        
        elif cmd == "status":
            embed = discord.Embed(
                title=f"🤖 {AGENT_NAME} v2 Status",
                color=discord.Color.green()
            )
            embed.add_field(name="Messages in context", value=str(len(context_history)))
            embed.add_field(name="Server count", value=str(len(self.guilds)))
            embed.add_field(name="Uptime", value="Running", inline=False)
            await message.channel.send(embed=embed)
        
        elif cmd == "clear":
            context_history.clear()
            await message.channel.send("🧹 Context cleared!")
        
        else:
            await message.channel.send(f"❓ Unknown command: {cmd}. Try {AGENT_PREFIX}help")
    
    async def generate_response(self, message: discord.Message):
        """Generate response using LLM"""
        # Typing indicator
        async with message.channel.typing():
            try:
                # Build context for LLM
                messages = [
                    {"role": "system", "content": f"You are {AGENT_NAME}, a helpful AI assistant."}
                ]
                
                # Add recent context
                for entry in context_history[-10:]:
                    role = "assistant" if entry["author"] == str(self.user) else "user"
                    messages.append({
                        "role": role,
                        "content": entry["content"]
                    })
                
                # Add current message
                messages.append({
                    "role": "user", 
                    "content": message.content
                })
                
                # Call LLM API
                response = await self.call_llm(messages)
                
                # Send response
                if response:
                    await message.channel.send(response)
                else:
                    await message.channel.send("🤔 I'm having trouble thinking right now. Try again later.")
            
            except Exception as e:
                print(f"Error generating response: {e}")
                await message.channel.send("🤖 Something went wrong. Please try again.")
    
    async def call_llm(self, messages: List[dict]) -> Optional[str]:
        """Call LLM API"""
        if not LLM_API_KEY:
            return "⚠️ LLM API not configured. Set LLM_API_KEY environment variable."
        
        try:
            import requests
            
            headers = {
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json"
            }
            
            data = {
                "model": "MiniMax-Text-01",
                "messages": messages,
                "max_tokens": 500,
                "temperature": 0.7
            }
            
            response = requests.post(
                LLM_API_URL,
                headers=headers,
                json=data,
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                return result.get("choices", [{}])[0].get("message", {}).get("content", "")
            else:
                print(f"LLM API error: {response.status_code} - {response.text}")
                return None
        
        except Exception as e:
            print(f"LLM call failed: {e}")
            return None


def main():
    """Main entry point"""
    if not DISCORD_TOKEN:
        print("❌ Error: SELENA_DISCORD_TOKEN not set!")
        print("   Create a Discord bot at https://discord.com/developers/applications")
        print("   And set the SELENA_DISCORD_TOKEN environment variable")
        sys.exit(1)
    
    print("🤖 Starting Selena v2 Discord Bot...")
    print(f"   Agent: {AGENT_NAME}")
    print(f"   Command prefix: {AGENT_PREFIX}")
    
    # Create and run bot
    intents = discord.Intents.default()
    intents.message_content = True  # Required for reading message content
    
    bot = SelenaBot(intents=intents)
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
