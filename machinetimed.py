#!/usr/bin/python3
# machinetimed: Forked from doord. In charge of access control and charging for job time on tools.
# Runs on a local server, interacts with a primary civicrm instance.  
'''
Create your own machinetimed-secrets.conf using the example.  A relatively easy way 
to make a secure connection to your primary civicrm host's mysql database is with 
autossh.  Redirect the remote port locally. Something like:
/usr/bin/autossh -M 0 -o "ServerAliveInterval 30" -o "ServerAliveCountMax 3" -i \
  /home/[local-user]/.ssh/id_rsa  -4  -L 3307:127.0.0.1:3306 user@[civicrm-host]  -t /bin/bash

A note about Error Codes:
Error codes are divided into two types that can be sent before login or after,
because these situations follow different flows in the UI. For instance The Goodbye 
screen is skipped, since there's no one to say goodbye to in the case of the former. 
Code > 128 (x80) is the logged in type. 

'''

import datetime
import os
import pythonmstk
import configparser
import json
import time
from flask import Flask, abort, request
import urllib3.contrib.pyopenssl
urllib3.contrib.pyopenssl.inject_into_urllib3()
import meetup.api
from decimal import *  

secrets_file = 'machinetimed-secrets.conf'

config = configparser.ConfigParser()
secrets_path = os.path.realpath(
    os.path.join(os.getcwd(), os.path.dirname(__file__)))
config.read(os.path.join(secrets_path, secrets_file))

machinetimed_host = config.get('machinetimed', 'host')
machinetimed_port= config.get('machinetimed', 'port')
api_key_enabled = config.get('machinetimed','api_key_enabled')
api_key = config.get('machinetimed', 'api_key')

log_level = int(config.get('mstk','log_level'))
meetup_group = config.get('meetup', 'meetup_group')
token = config.get('meetup', 'token')
meetup_enabled = config.get('meetup', 'meetup_enabled')

if meetup_enabled == "True":
  client = meetup.api.Client(token)

getcontext().prec = 9

# Initialize the Flask application
app = Flask(__name__)
#app.config['DEBUG'] = True
app.config.update(
    JSONIFY_PRETTYPRINT_REGULAR=False
)

class MachineTimed(pythonmstk.MstkServer):

   def merge_dicts(self,*dict_args):
       result = {}
       for dictionary in dict_args:
           result.update(dictionary)
       return result

   '''
   make_charge accepts job time (or qty) and merged dicts of balance and user as params
   '''
   
   def make_charge(self,job_time, params, **kwargs):
       self.debug_message(log_level, 6, "Charge params are %s" % params)
       rate = Decimal(params['rate']) * 100
       multiplier = rate / Decimal(60) 
       amount = int(round(int(job_time) * multiplier, 0))
       # non memebrs take from pocket_store only
       if params['member_status'] == "0":
          # check for per diem
          if params['perdiem_charge'] == 'True':
             amount = int(params['amount'])
             new_pocket_store = int(params['pocket_store']) - amount
             new_member_store = 0
             job_time = 0
             del params['amount']
          else:
             new_pocket_store = int(params['pocket_store']) - amount
             new_member_store = 0
       # members take from member_store first! then pocket store.
       # we want this to works for negative, better to give credit than to have an error that ruins material.
       elif int(params['member_status']) == 1:
          if amount > int(params['member_store']):
             self.debug_message(log_level, 3, "amount %s greater than member_store(!), also taking from pocket_store" % amount)
             remainder = amount - int(params['member_store'])
             new_member_store = 0
             new_pocket_store = int(params['pocket_store']) - remainder 
             print(new_pocket_store)
          else:
             new_member_store = int(params['member_store']) - amount
             new_pocket_store = params['pocket_store']
             self.debug_message(log_level, 3, "deducting amount %s from member_store, new member_store is %s" % (amount, new_member_store))
       charge_dict = {
                     "date":str(datetime.date.today()),
                     "datetime":str(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
                     "contact_id":params['contact_id'],
                     "is_debit":"1",
                     "job_time":job_time,
                     "machine_id":params['access_point'],
                     "rate":str(rate),
                     "amount":amount,
                     "member_store":new_member_store,
                     "pocket_store":new_pocket_store,
                     "prev_ledger_item":params['id'],
                     "prev_member_store":params['member_store'],
                     "prev_pocket_store":params['pocket_store'],
                     "notes":params['notes']
                      }
       try:
          charge_results = self.civicrm.create("LedgerItem", **charge_dict)
       except:
          machinetimed.debug_message(log_level, 0, "Charge failed!") 
          print(self.civicrm.create("LedgerItem", **charge_dict))
   
       charge_results = self.merge_dicts(params,charge_results[0])
   
       # check for over-drawn, not sure how often we'll hit /machine yet
       if int(new_pocket_store) < 0:
          self.debug_message(log_level, 3,"%s is over-drawn and has balance %s" % (params['display_name'], new_pocket_store))
          charge_results['error_code'] = 'x04'
          charge_results['access'] = 0
   
       del charge_results['contribution_id']
   
       return charge_results
   
   '''
       Retreive user's balance
   '''
   
   def get_current_balance_dict(self,user_dict, **kwargs):
       my_sort= "id DESC"
       my_limit= 1
       search_dict = {
                     "contact_id":user_dict['contact_id'],
                     "return":"contact_id,member_store,pocket_store,id",
                     }
       try:
            balance_results = self.civicrm.get("LedgerItem", limit=my_limit, sort=my_sort,  **search_dict )
       except:
           balance_results = self.civicrm.get("LedgerItem", **search_dict )
           print ("api failure for balance search")
       if not balance_results:
          self.debug_message(log_level, 3, "no job history for %s" % user_dict['display_name']) 
          balance_results = {"id":"0","contact_id":user_dict['contact_id'],"member_store":"0","pocket_store":"0"}
       else:
          # convert single itemed list to dictionary
          balance_results = balance_results[0]
          self.debug_message(log_level, 3, "balance found: %s" % json.dumps(balance_results) ) 
       return balance_results
   
   
   '''
      Test if there is currenly an X Open Hours meetup event
   '''
   
   def meetup_check(self,open_hours_type,error_code):
      # add try except for internet down situations. 
      try:
          events = client.GetEvents({'group_urlname':meetup_group,'page':'5','fields':{'duration'}})
      except:
          # internet prob
          # check if it's a Monday? 
          print('meetup prob')
          access = 0 
          error_code = 'x83'
          return access, error_code
      open_hours = 0
      for x in range(5):
        if events.results[x]['name'] == ('%s Open Hours' % open_hours_type):
          meetupStart = int(events.results[x]['time']/1000)
          open_hoursStart = meetupStart - 900
          open_hoursEnd = meetupStart + 14400
          if int(time.time()) > open_hoursStart and int(time.time()) < open_hoursEnd:
            open_hours = open_hours + open_hours

   # uncomment next line for simulating a meetup
   #   open_hours = 1
      self.debug_message(log_level,3,"open hours status is %s" % open_hours)
      return(open_hours, error_code)
                     

machinetimed  =  MachineTimed(secrets_path,secrets_file)

@app.route('/machine', methods = ['GET', 'POST'])
def accept_card_uid():
    # Find the requesting AP.
    client_ip = request.environ['REMOTE_ADDR']
    access_point = machinetimed.ap_lookup(client_ip) 
    print("access point uis %s" % access_point)
    if access_point['error_code'] != 'x00':
        abort(404)
        return str(404)
    accesspoint_id = access_point['id']

    if request.method == 'POST':
        try:
            received_apikey = request.form['apikey']
            if received_apikey != api_key:
               abort(404)
               return str(404)
            else:
               print('api key is %s' % received_apikey)
        except:
            abort(404)
            return str(404)

        card_serial = (request.form['uuid'])
        machinetimed.debug_message(log_level,6, "POST to /machine with card id: %s on the %s" % (card_serial,accesspoint_id))
        if len(card_serial) == 10 or 9 or 8:
           user_dict = machinetimed.card_lookup(card_serial,**access_point)
           balance_dict = machinetimed.get_current_balance_dict(user_dict)
           print(balance_dict)
           if user_dict['error_code'] == 'x80':
              # unknown card, no need to continue. Triming down response
              return str('{"display_name":"%s","access":"%s","error_code":"%s"}') % (user_dict['display_name'], user_dict['access'], user_dict['error_code'])
           # if meetup_enabled, elevate access if we're having a Laser Open Hours meetup.
           if meetup_enabled == "True":
              (meetup,user_dict['error_code']) = machinetimed.meetup_check(accesspoint_id,user_dict['error_code'])
              if meetup == 1:
                 machinetimed.debug_message(log_level, 3,"Elevating access during this meetup")
                 user_dict['access'] = 1
                 user_dict['error_code'] = "x00"
                 # Make sure non-member has at least the perdiem in their balance. 
                 if meetup == 1 and user_dict['member_status'] == "0":
                    machinetimed.debug_message(log_level, 3,"Non-member during meetup, now to check balance")
                    if int(balance_dict['pocket_store']) <= 2000:
                       machinetimed.debug_message(log_level, 3,"%s has an insuffient balance to continue" % user_dict['display_name'])
                       user_dict['access'] = 0
                       user_dict['error_code'] = 'x03'
              # Diminish access for non-members outside these times
              if meetup == 0 and user_dict['member_status'] == "0":
                 machinetimed.debug_message(log_level, 3,"Enforcing meetup restriction")
                 user_dict['access'] = "0"
                 user_dict['error_code'] = "x02"
              # or if there is no Internet access
           elif user_dict['error_code'] == "x83":
              user_dict['access'] = 0
           if int(balance_dict['pocket_store']) < 0:
              machinetimed.debug_message(log_level, 3,"%s is over-drawn by %s cents" % (user_dict['display_name'], balance_dict['pocket_store']))
              user_dict['error_code'] = 'x04'
              user_dict['access'] = '0'
           params = machinetimed.merge_dicts(balance_dict,user_dict) 
           return json.dumps(params)
        else:
           #return abort(401)  # 401 Unauthorized
           print ("wrong number of characters")
           print (card_serial)
           return str('{"access":"0"}')
    else:
        print ("Not a POST made to /machine")
        return str('{"access":"0"}')

@app.route('/machine/job', methods = ['GET', 'POST', 'HEAD'])
def accept_job():
    # Find the requesting AP.
    client_ip = request.environ['REMOTE_ADDR']
    access_point = machinetimed.ap_lookup(client_ip) 
    print("access point uis %s" % access_point)
    if access_point['error_code'] != 'x00':
        abort(404)
        return str(404)
    accesspoint_id = access_point['id']

    if request.method == 'POST':
        try:
            received_apikey = request.form['apikey']
            if received_apikey != api_key:
               abort(404)
               return str(404)
        except:
            abort(404)
            return str(404)

        card_serial = (request.form['uuid'])
        job_time = (request.form['jobtime'])
        user_dict = machinetimed.card_lookup(card_serial,**access_point)
        state = "is" if user_dict['member_status'] == "1" else "is not"
        if int(job_time) < 0:
          machinetimed.debug_message(log_level, 0, "NEGATIVE JOBTIME DETECTED: from %s on %s. Nice try." % (user_dict['display_name'], accesspoint_id))
          return str('{"access":"0","133th4x0rz":"0","l0lz":"1"}') 
        machinetimed.debug_message(log_level, 3, "accepting job from %s on %s, who %s a member" % (user_dict['display_name'], accesspoint_id,state))
        balance_dict  = machinetimed.get_current_balance_dict(user_dict)
        print(json.dumps(balance_dict))
        params = machinetimed.merge_dicts( balance_dict,user_dict) 
        if int(user_dict['member_status']) == 0 and (access_point['.non_member_perdiem'] != None or int(access_point['.non_member_perdiem']) != 0):
           print('non member perdiem is %s' % access_point['.non_member_perdiem'])
           # look for previous $perdiem jobs today and charge it if none found
           search_dict = {
                         "contact_id":str(user_dict['contact_id']),
                         "date":str(datetime.date.today()),
                         "amount":str(access_point['.non_member_perdiem']),
                          }
           try:
              search_results = civicrm.get("LedgerItem", **search_dict)
           except:
              return ('api failure!')
           params.update({"perdiem_charge":"False"})
           if not search_results: 
              machinetimed.debug_message(log_level, 3, "Charging non-member %s a %s per diem" % (user_dict['display_name'],access_point['non_member_perdiem']))
              params.update({"notes":"Non-member per deim charge."})
              params.update({"amount":access_point['.non_member_perdiem']})
              params.update({"perdiem":"True"})
              machinetimed.make_charge(1, params) 
              balance_dict = machinetimed.get_current_balance_dict(user_dict)
              params = machinetimed.merge_dicts( balance_dict,user_dict) 
              params['notes'] = "none"
              machinetimed.debug_message(log_level, 3, "Charging non-member %s for %s seconds jobtime on the %s" % (user_dict['display_name'],job_time,access_point['ap_short_name']))
              return json.dumps(make_charge(job_time,params))
           else:
              params['notes'] = "none"
              params.update({"rate":access_point['.non_member_rate']})
              machinetimed.debug_message(log_level, 3, "Charging non-member %s for %s seconds jobtime on the %s" % (user_dict['display_name'],job_time,access_point['ap_short_name']))
              return json.dumps(machinetimed.make_charge(job_time,params))
        elif int(user_dict['member_status']) == 1:
          params['notes'] = "null"
          params.update({"rate":access_point['.member_rate']})
          machinetimed.debug_message(log_level, 3, "Charging member %s for %s seconds jobtime on the %s" % (user_dict['display_name'],job_time,access_point['ap_short_name']))
          return json.dumps(machinetimed.make_charge(job_time,params))
    elif request.method == 'GET':
        print('this is GET')
        card_serial = request.args.get('uuid', '')
        user_dict = machinetimed.card_lookup(card_serial,**access_point)
        print(user_dict)
        if not request.args.get('page', ''):
          page = 1
        else:
          page = int(request.args.get('page', ''))
        my_offset =  (page - 1) * 15 
        my_limit = 15  # this is the number of records per page
        my_sort = "id DESC"
        search_results = None
        search_dict = {
                  "contact_id":user_dict['contact_id'],
                  }
        try:
           search_results = machinetimed.civicrm.get("LedgerItem", limit=my_limit, offset=my_offset, sort=my_sort, **search_dict)
        except:
           search_results = {} 
           pass
           # TODO test this condition in UI 
        return json.dumps(search_results)
        socket.disconnect
        abort(200) 

@app.route('/environment', methods = ['GET'])
def environment_query():
    # Find the requesting AP.
    client_ip = request.environ['REMOTE_ADDR']
    access_point = machinetimed.ap_lookup(client_ip) 
    print("access point is %s" % access_point)
    if access_point['error_code'] != 'x00':
        abort(404)
        return str(404)
    accesspoint_id = access_point['id']

    if request.method == 'GET':
        received_apikey = request.args.get('apikey', '')
        print("received_apikey is %s " % received_apikey)
        try:
            received_apikey = request.args.get('apikey', '')
            if received_apikey == api_key:
               search_dict = {
                 "aco":accesspoint_id,
               }
               try:
                  error_dicts = machinetimed.civicrm.get("ApErrorCodes", **search_dict )
                  error_dict = {}
                  for dict in error_dicts:
                     error_dict[dict['error_key']] = dict['error_value']
               except:
                  # not all tools will have error codes
                  pass
               search_dict = {
                 "return": "id,ap_short_name"
               } 
               try:
                  ap_search = machinetimed.civicrm.get("AccessPoints", **search_dict )
               except:
                  return str("ap lookup failed")
               environment = {}
               for value in ap_search:
                  try:
                     environment[value['id']] = value['ap_short_name'] 
                  except:
                    pass
               environment.update({'access_point_id':str(accesspoint_id)})
               environment = machinetimed.merge_dicts(environment,error_dict)
               print( json.dumps(environment))
               return json.dumps(environment)
            else:
               # wrong key
               abort(404)
        except:
            #failed apikey access
            abort(404)

@app.after_request
def apply_caching(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate" # HTTP 1.1.
    response.headers["Pragma"] = "no-cache" # HTTP 1.0.
    response.headers["Expires"] = "0" # Proxies.
    response.headers["Content-Type"] = "application/json"
    return response



if __name__ == '__main__':
  app.run(
        host=machinetimed_host,
        port=int(machinetimed_port)
#        ssl_context='adhoc',
  )

