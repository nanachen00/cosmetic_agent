#!/usr/bin/env python3
"""Create or validate a PRIVATE fixed-account file. Does not call any API."""
from __future__ import annotations
import argparse
import getpass
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from common import Refuse,atomic_json
from auth_clients import load_accounts


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output',default=str(Path(__file__).resolve().parent/'.private/accounts.json'))
    ap.add_argument('--count',type=int,default=16)
    ap.add_argument('--check-only',action='store_true')
    a=ap.parse_args();os.umask(0o077);p=Path(a.output).absolute()
    if not 1<=a.count<=16:raise Refuse('count must be 1..16')
    if a.check_only:
        accounts=load_accounts(p,required=a.count)
        print('[ACCOUNTS CHECK PASS]',len(accounts),'configured; required',a.count)
        print('Aliases:',','.join(x.alias for x in accounts));return
    if not sys.stdin.isatty():raise Refuse('Interactive terminal required; do not pipe passwords on command line')
    if p.exists() or p.is_symlink():raise Refuse('Refusing to overwrite an existing account file; use --check-only or edit privately')
    p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if p.parent.stat().st_mode & 0o077:raise Refuse('Private directory must have mode 700')
    rows=[]
    for i in range(a.count):
        alias=f'A{i+1:02d}'
        print(alias)
        username=input('  Username: ').strip()
        password=getpass.getpass('  Password (not displayed): ')
        confirmation=getpass.getpass('  Confirm password: ')
        if password!=confirmation:raise Refuse('Passwords do not match; nothing saved')
        expected=input('  Expected user_id (optional, Enter to validate against login/session): ').strip() or None
        rows.append({'alias':alias,'username':username,'password':password,'expected_user_id':expected})
    # Temp file validation before commit; never print submitted credentials.
    temp=p.parent/('.accounts-pending-'+str(os.getpid())+'.json')
    if temp.exists():raise Refuse('Private pending file exists; refusing overwrite')
    try:
        atomic_json(temp,{'version':1,'accounts':rows});load_accounts(temp,required=a.count)
        # Link is an exclusive commit so a concurrent writer cannot be overwritten.
        os.link(temp,p);print('[SAVED]',str(p))
    finally:
        temp.unlink(missing_ok=True)
    print('Keep this file private. Do not upload it or include it in result archives.')

if __name__=='__main__':
    try:main()
    except (Refuse,KeyboardInterrupt,EOFError) as exc:
        print('[STOP]',str(exc) if isinstance(exc,Refuse) else 'Cancelled; no account file committed',file=sys.stderr)
        raise SystemExit(1)
