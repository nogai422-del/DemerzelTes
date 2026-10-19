"""Веб-настройки и ручное снятие донатов."""
import hmac
import secrets
import time
from flask import Blueprint, redirect, render_template, request, session

from bot.database import db
from bot.donations import revoke_donation_grant, revoke_all_donation_grants
from bot.donation_revoke_settings import COMMANDS, ensure_donation_revoke_schema, log_revoke_action

donation_revoke_bp = Blueprint('donation_revoke', __name__)

def _auth():
    return 'username' in session

def _csrf_ok():
    sent = request.form.get('csrf_token','')
    expected = session.get('csrf_token','')
    return bool(sent and expected and hmac.compare_digest(sent, expected))

def _checked(name):
    return 1 if request.form.get(name) == '1' else 0

async def _set_media_level(chat_id: int, user_id: int, level: int):
    async with db() as cur:
        await cur.execute("SELECT 1 FROM chat_users WHERE chat_id=? AND user_id=?", (chat_id,user_id))
        if await cur.fetchone():
            await cur.execute("UPDATE chat_users SET permission_level=? WHERE chat_id=? AND user_id=?", (level,chat_id,user_id))
        else:
            await cur.execute("INSERT INTO chat_users(chat_id,user_id,score,level,permission_level) VALUES(?,?,0,0,?)", (chat_id,user_id,level))

@donation_revoke_bp.route('/donation_revoke', methods=['GET','POST'])
async def donation_revoke():
    if not _auth(): return redirect('/login')
    await ensure_donation_revoke_schema()
    session.setdefault('csrf_token', secrets.token_urlsafe(32))
    if request.method == 'POST':
        if not _csrf_ok(): return redirect('/donation_revoke?csrf=0')
        async with db() as cur:
            await cur.execute("""UPDATE donation_revoke_global_settings SET
                access_mode=?, allowed_user_ids=?, allow_reply=?, allow_user_id=?,
                notify_target=?, notify_chat=?, require_delmax_confirmation=?,
                media2_behavior=?, success_template=?, missing_template=?, error_template=? WHERE id=1""", (
                request.form.get('access_mode','admins'), request.form.get('allowed_user_ids',''),
                _checked('allow_reply'), _checked('allow_user_id'), _checked('notify_target'),
                _checked('notify_chat'), _checked('require_delmax_confirmation'),
                request.form.get('media2_behavior','downgrade'), request.form.get('success_template',''),
                request.form.get('missing_template',''), request.form.get('error_template','')
            ))
            for key in COMMANDS:
                await cur.execute("UPDATE donation_revoke_commands SET enabled=?, command_name=?, aliases=? WHERE command_key=?", (
                    _checked(f'cmd_{key}_enabled'), request.form.get(f'cmd_{key}_name', COMMANDS[key]['command']).strip().lstrip('/'),
                    request.form.get(f'cmd_{key}_aliases','').strip(), key
                ))
        return redirect('/donation_revoke?saved=1')

    async with db() as cur:
        await cur.execute('SELECT * FROM donation_revoke_global_settings WHERE id=1')
        settings = dict(await cur.fetchone())
        await cur.execute('SELECT * FROM donation_revoke_commands')
        commands = {r['command_key']: dict(r) for r in await cur.fetchall()}
        await cur.execute("""SELECT cu.chat_id,cu.user_id,COALESCE(cu.permission_level,0) permission_level,
            GROUP_CONCAT(CASE WHEN dg.valid_until > CAST(strftime('%s','now') AS INTEGER) THEN dg.category END) donations
            FROM chat_users cu LEFT JOIN donation_grants dg ON dg.chat_id=cu.chat_id AND dg.user_id=cu.user_id
            GROUP BY cu.chat_id,cu.user_id ORDER BY cu.user_id DESC LIMIT 300""")
        users = [dict(r) for r in await cur.fetchall()]
        await cur.execute('SELECT * FROM donation_revoke_audit ORDER BY id DESC LIMIT 100')
        audit = [dict(r) for r in await cur.fetchall()]
    return render_template('donation_revoke.html', settings=settings, commands=commands, definitions=COMMANDS,
        users=users, audit=audit, csrf_token=session['csrf_token'], saved=request.args.get('saved'), csrf=request.args.get('csrf'), action=request.args.get('action'))

@donation_revoke_bp.post('/donation_revoke/action')
async def donation_revoke_action():
    if not _auth(): return redirect('/login')
    if not _csrf_ok(): return redirect('/donation_revoke?csrf=0')
    try:
        chat_id = int(request.form['chat_id']); user_id = int(request.form['user_id']); action = request.form['action']
        details = ''
        if action in ('emoji','voice','video_note','tag'):
            existed = await revoke_donation_grant(chat_id,user_id,action); details = 'removed' if existed else 'missing'
        elif action == 'media':
            await _set_media_level(chat_id,user_id,0); details='media->0'
        elif action == 'media2':
            async with db() as cur:
                await cur.execute('SELECT COALESCE(permission_level,0) FROM chat_users WHERE chat_id=? AND user_id=?',(chat_id,user_id)); row=await cur.fetchone(); old=int(row[0] if row else 0)
            await _set_media_level(chat_id,user_id,1 if old>=2 else old); details=f'media {old}->{1 if old>=2 else old}'
        elif action == 'all':
            removed=await revoke_all_donation_grants(chat_id,user_id); await _set_media_level(chat_id,user_id,0); details='removed:'+','.join(removed)
        else: raise ValueError('unknown action')
        await log_revoke_action(source='web', admin_id=None, admin_name=session.get('username','web'), chat_id=chat_id,target_id=user_id,action=action,details=details)
        return redirect('/donation_revoke?action=ok')
    except Exception as exc:
        return redirect('/donation_revoke?action=error')
