#!/usr/bin/env python3

# GNU Radio Python Flow Graph
# Title: SAS USRP Transmitter
# Author: Cam Makin
# For Research Efforts: Wireless@VT
# Description: Implementiation of an SDR Tx for SAS control. This flowgraph is the base of the TX Python script that will be further modified to include sockets and other SAS API requirements.
# GNU Radio version: 3.8.1.0
# Generated October 7, 2020
# Last Updated: 02/13/2021

# TODO: Dynamic Socket Addressing (Want to be able to switch address/port of socket on the fly)
# TODO: Decide how SAS assigned info (i.e. cbsdId) is stored
# TODO: Create some command line keyword that works at all instances to exit from the prompt...
# TODO: Create a function to handle the "response" field from all the SAS responses
# TODO: Maybe add menu option to view all history to see what has been done and their responses
#./socket_tx.py -p 5000 -a "127.0.0.1"


import sys
from argparse import ArgumentParser
from gnuradio import uhd
import socketio
import json
from usrps import Node
from WinnForum import *		# File containing object definitions used
from cmd_prompts import * 	# User defined library for cmd prompts
import threading


# Globals
# blocked: Used to ensure recieved socket messages display on the terminal before the main menu blocks the command-line interface .
# This variable is set to 'True' right before a client emit with an expected response executes. The response will set it back to False.
__blocked = False

# sim_mode: Used to ensure functions do not attempt blocking user-input features.
__sim_mode = False

# Tracks time since last heartbeatRequest is sent.
__heartbeatTimer = None

# tempNodeList: used to hold the order of which nodes are sending requests...
#	...so that when the response comes in, data can be assigned properly
tempNodeList = []

# created_nodes: used to hold all created CRTS USRP Nodes objects
created_nodes = []

# registered_nodes: nodes that have requested to be registered with the SAS.
registered_nodes = []

# Parser extracts command line flags/parameters  
parser = ArgumentParser(description='SAS USRP RX Interface Script - Provide a server address and port in order to connect to the SAS.')

# Socket Params----------------------------------------------------------------------------
parser.add_argument('-a','--address',\
		help='Server address. Example: -a \'127.0.0.1\'',\
		default='127.0.0.1')
parser.add_argument('-p','--port',\
		help='Server port. Example: -p 65432',\
		default='5000')
#------------------------------------------------------------------------------------------

# Use Simulation File----------------------------------------------------------------------
parser.add_argument('-s','--sim',\
		help='Simulation File path (realative to socket_to_sas.py). Example: -s simulations/sim_one.json',\
		default=None)
#------------------------------------------------------------------------------------------

# Helper Functions-------------------------------------------------------------------------
def printCreatedNodes():
	"""
	Prints out create_nodes to terminal
	"""
	for node in created_nodes:
		node.printInfo()

def isVaildInt(value):
	"""
	Returns True if a value can be casted to an int

	Parameters
	---------
	value : any data type
		Data to check if it can be casted to an int

	Returns
	-------
	isValidInt : boolean
		True if 'value' can be casted to an int, else False
	"""
	try:
		int(value)
		return True
	except ValueError:
		print("'" + value + "' is not a vaild int")
	return False

def _grabPossibleEntry(entry, key):
	"""
	Takes a dictionary entry and checks to see if it exists (e.g. check for entry[key]).

	Parameters
	----------
	entry : dictionary
		Dictonary to check for 'key'
	key : string
		String key for the dictonary

	Returns
	-------
	value : various types
		If entry exists, returns entry, otherwise return None
	"""
	try:
		return entry[key]
	except KeyError:
		return None

def send_params(clientio, node):
	"""
	Collects current node parameters and sends to host via socket as JSON

	Parameters
	----------
	clientio : socket object
		socket connection to host
	node : SDR object
		USRP object to gather operating parameters from
	"""

	data = {
		"SDR Address": node.get_SDR_Address(),
		"Center Frequency": node.get_freq(),
		"Gain": node.get_gain(),
		"Sample Rate": node.get_signal_amp(),
		"Signal Amplitude": node.get_signal_amp(),
		"Waveform": str(node.get_waveform()),
		"Status": node.get_status()
	}
	payload = json.dumps(data)
	clientio.emit('getNodeParams', payload)

def updateRadio(node, params):
	"""
	Updates radio operating parameters

	Parameters
	----------
	node : SDR object
		USRP object to gather operating parameters from
	params : dictionary
		JSON package of parameters to update
	"""

	# check params for every key:value pair
	params = json.loads(params)
	if "freq" in params:
		node.set_freq(float(params['freq'])*1e6)
	if "gain" in params:
		node.set_gain(int(params['gain']))
	if "samplerate" in params:
		node.set_sample_rate(int(params['samplerate'])*1e6)
	if "sigamp" in params:
		signalAmp = float(params['sigamp'])
		if signalAmp > 1:
			signalAmp = 1
		node.set_signal_amp(signalAmp)
	if "waveform" in params:
		waveform  = (params['waveform'])
		node.set_waveform(waveform)
	if "device" in params:
		node.set_SDR_Address(params['device'])
	if "status" in params:
		node.set_status(params['status'])

	# Ack to server with new params
	# send_params(clientio, node)

def findTempNodeByCbsdId(cbsdId):
	"""
	SAS Responses utilize cbsdId to ID which nodes go with which responses.
	This function accepts a cbsdId and returns the Node object is goes with.

	Parameters
	----------
	cbsdId : string
		CBSD ID of a desired Node awaiting a SAS response (i.e a node in tempNodeList)

	Returns
	-------
	node : Object
		The Node with the same cbsdId. Will return 'None' if no Node has the cbsdId.
	"""
	for node in tempNodeList:
		if(node.getCbsdId() == cbsdId):
			return node
	return None

def findCreatedNodeByIp(address):
	"""
	Returns a Node with the provided IP Address

	Parameters
	----------
	address : string
		IP Address of a created_node

	Returns
	-------
	node : Node object
		Node with the desired IP Address
	"""
	for node in created_nodes:
		if(address == node.getIpAddress()):
			return node
	return None

def reqAddressToNode(request, mustBeRegistered=True):
	"""
	This finds an IP address from a Sim request, and returns the Node object with the same IP.

	Since the simulation file cannot identify Nodes by their cbsdIds that are dynamically assigned,
	the sim file uses the Node IP addresses to idenetify them. 
	This funciton helps by using the Node IP and returning the Node object, so that more info is available.
	This is desirable in all the Requests functions (hence this function beginning with 'req').

	Parameters
	----------
	request : dictionary
		A single request from a simulation file
	muBeRegistered : boolean
		If True, only return back a Node obj if it has a cbsdId (i.e. if it is already registered)

	Returns
	-------
	node : Node object
		If a node match is found, return the Node, else return None
	"""
	address = _grabPossibleEntry(request, "nodeIp")
	if(not address):
		print("No nodeIp address found.")
		return None
	node = findCreatedNodeByIp(address)
	if(not node):
		print("No Created Node was found with the IP Address: '" + address + "'.")
		return None
	if(mustBeRegistered and not node.getCbsdId()):
		print("Node found but not yet registered.")
		return None
	return node

def _isValidResponse(response):
	"""
	This checks to see is the Response object provided by the SAS indicates a sucessfull request.
	Response data will also be printed out in here, regardless of responseCode ("0").

	Parameters
	----------
	response : dictonary 
		A single Response object data

	Returns
	-------
	isValid : boolean
		Will return True if the response code is "0", otherwise False
	"""
	if(response):
		responseCode = _grabPossibleEntry(response, "responseCode")
		if(not responseCode):
			print("SAS Error: No response code provided.")
			return False
		else:
			print("Response Code: " + responseCode)
		responseMessage =  _grabPossibleEntry(response, "responseMessage")
		if(responseMessage):
			print("Response Message: " + responseMessage)
		responseData = _grabPossibleEntry(response, "responseData")
		if(responseData):
			print("Response Data: " + responseData)
		if(responseCode != "0"):
			print("SAS Error: Registration Unsuccessful (non-zero response code).")
			return False
	else:
		print("SAS Error: No reponse object found.")
		return False
	return True
# End Helper Functions---------------------------------------------------------------------

# Create Node------------------------------------------------------------------------------
def simCreateNode(requests):
	"""
	Creates node(s) based on simulation file

	TODO: Make sure the data can create a vaild USRP
	TODO: What params would RX need instead?
	TODO: Decide what KeyErrors should hault node creation

	Parameters
	----------
	requests : array of dictionaries 
		Node data for creation

	Returns
	-------
	arr : array of Node object(s)
	"""
	arr = []
	for request in requests:
		usrpMode = address = centerFreq = gain = sampleRate = signalAmp = waveform = None

		usrpMode = _grabPossibleEntry(request, "mode")
		if(not usrpMode):
			print("No usrpMode found for simCreateNode. Node not created.")
			continue # Move onto next 'request'

		address = _grabPossibleEntry(request, "address")
		if(not address):
			print("No address found for simCreateNode. Node not created.")
			continue
		# Ensure IP address is not in an already created_node that may be inactive
		if(findCreatedNodeByIp(address)):
			print("IP Address already belongs to a created Node. Node not created.")
			continue
			
		centerFreq = _grabPossibleEntry(request, "centerFreq")
		if(not centerFreq):
			print("No centerFreq found for simCreateNode. Node not created.")
			continue
		gain = _grabPossibleEntry(request, "gain")
		if(not gain):
			print("No gain found for simCreateNode.")
			#TODO Is this required?
		sampleRate = _grabPossibleEntry(request, "sampleRate")
		if(not sampleRate):
			print("No sampleRate found for simCreateNode.")
			#TODO Is this required?
		signalAmp = _grabPossibleEntry(request, "signalAmp")
		if(not signalAmp):
			print("No signalAmp found for simCreateNode.")
			#TODO
		waveform = _grabPossibleEntry(request, "waveform")
		if((not waveform) and (usrpMode == "TX")):
			print("No waveform found for simCreateNode.")
			#TODO
		
		node = Node(address)
		node.setOperationMode(usrpMode)

		if(usrpMode == "TX"):
			node.setTxUsrp(centerFreq, gain, sampleRate, signalAmp, waveform)
		elif(usrpMode == "RX"):
			node.setRxUsrp("")# TODO
		else:
			#Invalid usrpMode provided
			continue
		arr.append(node)
	return arr

def cmdCreateNode():
	"""
	Walks a user through the command line to configure a USRP node.
	Appends a node to the global created_nodes list

	TODO: "How many nodes do you wanna create?"
	"""
	sdrAddr = promptUsrpIpAddr()
	node = Node(sdrAddr)
	usrpMode = promptUsrpMode()
	cFreq = promptUsrpCenterFreq()
	if(usrpMode == 'TX'):
		# TODO: find min/max for all of these
		usrpGain = promptUsrpGain()
		sampleRate = promptUsrpSampleRate()
		signalAmp = promptUsrpSignalAmp()
		waveform = promptUsrpWaveform()
		node.setTxUsrp(cFreq, usrpGain, sampleRate, signalAmp, waveform) # Create instance of Tx with given params
	elif(usrpMode == 'RX'):
		pass # Big TODO
	else:
		print("Error")
	return [node]

def createNode(requests=None):
	"""
	Function always called when creating new Nodes. Appends create_nodes array with new Nodes.

	Parameters
	----------
	requests : array of Requests (optional)
		If simulation file is calling this function, requests is an array of >=1 node data.
		If this is being called by the cmd-line interface, requests should be 'None'
	"""
	global created_nodes
	if(__sim_mode):
		nodes = simCreateNode(requests)
	else:
		nodes = cmdCreateNode()
	for node in nodes:
		created_nodes.append(node)
# End Create Node--------------------------------------------------------------------------

# Registation Request----------------------------------------------------------------------
def simRegistrationReq(requests):
	"""
	Simulation file provides data to create a Registration Request.

	Since there may be multiple registration requests at once, there is a for loop. 
	The value of 'request' should be a single registration request. 

	Parameters
	----------
	requests : array of dictionaries
		Registration Request data
	"""
	arr = []
	global tempNodeList
	tempNodeList = []
	iter = -1
	for request in requests:
		iter = iter + 1
		cbsdSerialNumber = userId = fccId = callSign = cbsdCategory = cbsdInfo = airInterface = None
		installationParam = measCapability = groupingParam = cpiSignatureData = vtParams = None
		print("Creating Registration Request [" + str(iter+1) + "]:")
		node = reqAddressToNode(request, False)
		if(not node):
			print("Registration Request invalid.")
			continue
		cbsdSerialNumber = node.getSerialNumber()
		if(not cbsdSerialNumber):
			print("No cbsdSerialNumber found for the node with IP Address: '" + node.getIpaAddress + "'. Registration Request invalid.")
			continue
		userId = _grabPossibleEntry(request, "userId")
		if(not userId):
			print("No userId found for simRegistrationReq. Registration Request invalid.")
			continue
		fccId = _grabPossibleEntry(request, "fccId")
		if(not fccId):
			print("No fccId found for simRegistrationReq. Registration Request invalid.")
			continue
		callSign = _grabPossibleEntry(request, "callSign")
		cbsdCategory = _grabPossibleEntry(request, "cbsdCategory")
		# TODO: Determine the proper cbsdCategories
		# if(not cbsdCategory):
		# 	print("No cbsdCategory provided. Registration Request invalid.")
		# 	continue
		cbsdInfo = _grabPossibleEntry(request, "cbsdInfo")
		airInterface = _grabPossibleEntry(request, "airInterface")
		# TODO: Determine the proper airInterfaces for the USRPs
		# if(not airInterface):
		# 	print("No airInterface provided. Registration Request invalid.")
		# 	continue
		installationParam = _grabPossibleEntry(request, installationParam)
		# TODO: installationParam is condiitonal. Determine when it is needed.
		# if(not installationParam):
		# 	print("No installationParam provided. Registration Request invalid.")
		# 	continue
		measCapability = _grabPossibleEntry(request, "measCapability")
		# TODO: measCapability is conditional. Determine when it is required.
		# This may be "RECEIVED_POWER_WITH_GRANT" if a Node can RX while TX-ing
		# This may be "RECEIVED_POWER_WITHOUT_GRANT" if a Node will RX when not TX-ing
		# This may be empty ("") if the Node has no RX ability
		# This is an array, and a Node may be assigned both values (e.g. always RX)
		# if(not measCapability):
		# 	print("No measCapability provided. Registration Request invalid.")
		# 	continue
		groupingParam = _grabPossibleEntry(request, "groupingParam")
		cpiSignatureData = _grabPossibleEntry(request, "cpiSignatureData")
		vtParams = _grabPossibleEntry(request, "vtParams")

		tempNodeList.append(node)
		arr.append(RegistrationRequest(userId, fccId, 
			cbsdSerialNumber, callSign, cbsdCategory,
			cbsdInfo, airInterface, installationParam, 
			measCapability, groupingParam, cpiSignatureData, vtParams).asdict())
	return arr		
	
def configRegistrationReq():
	"""
	Pulls Registration Request Info from a file the user selects

	TODO
	"""
	return []

def cmdRegistrationReq():
	"""
	Provides Command Line Prompts for a user to create Registration Request(s)
 
	Note: UHD Lib provides the serial, addr, and model ('type' for the uhd lib) for all usrps. Nodes with an FPGA includes the fpga.
		  Some 'type' matches with their 'product'. If there is a 'product', it is the same as the 'type' (e.g. x300).
		  When 'product' doesn't exist, it seems to be of type 'usrp2' 

		 TODO Change from "How many ..." to "Do you wanna do another?") && Is type USRP model?
	"""
	arr = []
	global tempNodeList
	tempNodeList = []
	num = promptNumOfRequests("How many Registration Requests would you like to create at this moment?: ")
	for x in range(num):
		userId = input("Enter User ID: ")
		fccId = input("Enter FCC ID: ")
		cbsdSerialNumber = promptCbsdSerial(created_nodes) # TODO: Redo this so that array holds node
		tempNodeList.append(cbsdSerialNumber)
		callSign = input("Enter Call Sign (Optional - Press Enter to Skip): ")
		cbsdCategory = promptCbsdCategory()
		cbsdInfo = promptCbsdInfo(cbsdSerialNumber, created_nodes)
		airInterface = promptAirInterface()
		installationParam = None
		installationInfoSelector = getSelectorBoolean(input("Do you want to enter Device Installation Information (Y)es or (N)o: "))
		if(installationInfoSelector):
			installationParam = promptInstallationParam()
		measCapability = getMeasCapabilityFromUser() #TODO: Need to ask Xavier about these capabilities
		groupingParam = None
		groupingParamSelector = getSelectorBoolean(input("Would you like to enter Grouping Parameter Info? (Y)es or (N)o: "))
		if(groupingParamSelector):
			quantity = int(input("How many groups do you want to create for this node?: ")) # TODO: Ensure non-negative (can this be 0?)
			for num in range(quantity):
				print("Node " + (num+1) + ":")
				print("Allowed Group Types: INTERFERENCE_COORDINATION. Select a combination below:")
				print("1. INTERFERENCE_COORDINATION")
				groupType = input("Selection: ")
				groupId = input("Enter Group ID: ") #** Do I add namespace after user input?
				groupingParam = [GroupParam(groupType, groupId)]
		arr.append(RegistrationRequest(userId, fccId, 
		cbsdSerialNumber, callSign, cbsdCategory,
		cbsdInfo, airInterface, installationParam, 
		measCapability, groupingParam, cpiSignatureData=None).asdict())
	return arr

def registrationRequest(clientio, payload=None):
	"""
	Function that should always be called for a Registration Request

	Parameters
	----------
	clientio : socket Object
		Socket connection to the SAS
	payload : array of Request(s) data
		Only used if the sim file is calling this funciton
	"""

	if(__sim_mode):
		arrOfRequest = simRegistrationReq(payload)
	else:
		while(True):
			data_source = input("Would you like to manually enter the registraion info or load from a file? (E)nter or (L)oad: ")
			if(data_source == 'E' or data_source == 'e'):
				arrOfRequest = cmdRegistrationReq() # Prompt User
				break
			elif(data_source == 'L' or data_source == 'l'):
				arrOfRequest = configRegistrationReq() # load config file
				break
			elif(data_source == 'exit'):
				return
			else:
				print("Invalid Entry... Please enter 'E' for Manual Entry or 'L' to load from a config file...")
	# Need to save list of nodes being registered...
	
	payload = {"registrationRequest": arrOfRequest}
	clientio.emit("registrationRequest", json.dumps(payload))

def handleRegistrationResponse(clientio, data):
	"""
	Receives data from SAS after a Registration Request is made

	TODO: If SAS doesnt give all required info, then ignore the fact that we sent a request

	Parameters
	----------
	clientio : socketio Object
		Socket connection
	data : JSON string
		Registration response data
	"""
	json_data = json.loads(data)
	iter = -1 # Increment happens at beginning of loop, so start with -1 to have 0 for the 1st loop

	regResponses = _grabPossibleEntry(json_data, "registrationResponse")
	if(not regResponses):
		print("SAS Error: Unreadable data. Expecting JSON formatted payload. Registration invalid.")
		return
	else:
		print("Registration Response Received")
	for regResponse in regResponses:
		iter = iter + 1 # Must increment at beginning because we may `continue` at any point
		print("Registration Response [" + str(iter+1) + "]:")

		response = _grabPossibleEntry(regResponse, "response")
		if(not _isValidResponse(response)):
			print("Registration invalid.")
			continue
		
		cbsdId = _grabPossibleEntry(regResponse, "cbsdId")
		if(cbsdId):
			node = tempNodeList[iter]
			node.setCbsdId(cbsdId)
			print("Node with IP Address: '" + node.getIpAddress() +"' is given CBSD ID# : '" + cbsdId +"'.")
		else:
			print("SAS Error: No cbsdId provided. Registration invalid.")
			continue
		
		measReportConfig =  _grabPossibleEntry(response, "measReportConfig")
		if(measReportConfig):
			if(not isinstance(measReportConfig, list)):
				measReportConfig = [measReportConfig]
			print("Measurment Report Configuration(s) Assigned: " + measReportConfig)
			node.setMeasReportConfig(measReportConfig)
		# TODO Update RX USRP with these params
		# TODO Do we go right in and start RX?
# End Registation Request------------------------------------------------------------------

# Spectrum Inquiry Request----------------------------------------------------------------
def simSpectrumInquiryReq(requests):
	"""
	Simulation file provides info on what spectum info to request from SAS

	Parameters
	----------
	requests : array of dictionaries
		Spectrum Inquiry Request data
	"""
	arr = []
	global tempNodeList
	tempNodeList = []
	iter = -1
	for request in requests:
		iter = iter + 1
		print("Spectrum Inquiry Request [" + str(iter+1) + "':")
		node = reqAddressToNode(request)
		if(not node):
			print("Spectrum Inquiry Request invalid.")
			continue
		cbsdId = node.getCbsdId()
		if(not cbsdId):
			print("No cbsdId found for the node with IP Address: '" + node.getIpAddress() + "'. Spectrum Inquiry Request invalid.")
			continue
		inquiredSpectrum = _grabPossibleEntry(request, "inquiredSpectrum")
		measReport = _grabPossibleEntry(request, "measReport")
		# TODO: measReport must be fake if it is getting passed in from the sim.json, or else
		# there must be a function call to pull real RX data at this point
		# TODO: measReport is required before a Node makes its first Grant request
		# Possibly ensure that if this is the first Spectrum Inquiry, that it includes measReport
		tempNodeList.append(node)
		arr.append(SpectrumInquiryRequest(cbsdId, inquiredSpectrum, measReport))
	return arr

def configSpectrumInquiryReq():
	"""
	TODO
	"""
	return [None]

def cmdSpectrumInquiryReq():
	"""
	Creates a Spectrum Inquiry Requests via command line input and returns a Spectrum Inquiry Request object
	"""
	arr = []
	# Print registered nodes' CBSD ID's
	print("Registered CBSD IDs:")
	for node in registered_nodes:
		print("\t"+node.get_CbsdId())
	cbsdId = input("Enter CBSD of node you want to use for the inquiry: ")
	inquiredSpectrum = promptFrequencyRange()
	provideRcvdPowerMeas = getSelectorBoolean(input("Do you want to provide Received Power Measurments to the SAS? (Y)es or (N)o: "))
	measReport = None
	if(provideRcvdPowerMeas):
		rcvdReport = promptRcvdPowerMeasReport()
		measReport = MeasReport([rcvdReport])
	arr.append(SpectrumInquiryRequest(cbsdId, inquiredSpectrum, measReport).asdict())
	return arr

def spectrumInquiryRequest(clientio, payload=None):
	"""
	Sends Spectrum Inquiry Request to the SAS
	"""
	arrOfRequest = None
	if(__sim_mode):
		arrOfRequest = simSpectrumInquiryReq(payload)
	while(True):
		dataSource = input("Would you like to manually enter the Spectrum Inquiry Request info or load from a file? (E)nter or (L)oad: ")
		if(dataSource == 'E' or dataSource == 'e'):
			arrOfRequest = cmdSpectrumInquiryReq() # Prompt User
			break
		elif(dataSource == 'L' or dataSource == 'l'):
			arrOfRequest = configSpectrumInquiryReq() # load config file
			break
		elif(dataSource == 'exit'):
			return
		else:
			print("Invalid Entry... Please enter 'E' for Manual Entry or 'L' to load from a config file...")
	payload = {"spectrumInquiryRequest": arrOfRequest}
	clientio.emit("spectrumInquiryRequest", json.dumps(payload))

def handleSpectrumInquiryResponse(clientio, data):
	"""
	Handles Spectrum Inquiry response from the SAS
	"""
	jsonData = json.loads(data)
	for SIResponse in jsonData["spectrumInquiryResponse"]:
		print(SIResponse)
		if(SIResponse["cbsdId"]):
			cbsdId = SIResponse["cbsdId"]
		if(SIResponse["availableChannel"]):
			availableChannel = SIResponse["availableChannel"]
		if(SIResponse["response"]):
			response = SIResponse["response"]
			if(response["responseCode"]):
				responseCode = response["responseCode"]
				if(isVaildInt(responseCode)):
					print("Response Code " + int(responseCode)+ ": " + responseDecode(int(responseCode)))
			if(response["responseMessage"]):
				responseMessage = response["responseMessage"]
			if(response["responseData"]):
				responseData = response["responseData"]
# End Spectrum Inquiry Request------------------------------------------------------------

# Grant Request---------------------------------------------------------------------------
def simGrantReq(requests):
	"""
	Function for simulation file to create a Grant request

	Parameters
	----------
	requests : array of dictionaries
		Grant Request data
	"""
	arr = []
	global tempNodeList
	tempNodeList = []
	iter = -1
	for request in requests:
		iter = iter + 1
		cbsdId = operationParam = measReport = vtGrantParams = None
		print("Grant Request [" + str(iter+1) + "':")
		node = reqAddressToNode(request)
		if(not node):
			print("Grant Request invalid.")
			continue
		cbsdId = node.getCbsdId()
		if(not cbsdId):
			print("No cbsdId found for the node with IP Address: '" + node.getIpAddress() + "'. Grant Request invalid.")
			continue

		measReport = _grabPossibleEntry(request, "measReport")

		operationParam = _grabPossibleEntry(request, "operationParam")
		if(not operationParam):
			print("No opeartionParam found. Grant Request invalid.")
			continue

		vtGrantParams = _grabPossibleEntry(request, "vtGrantParams")

		tempNodeList.append(node)
		arr.append(GrantRequest(cbsdId, operationParam, measReport, vtGrantParams).asdict())
	return arr

def configGrantReq():
	"""
	TODO
	"""
	return [None]

def cmdGrantReq():
	"""
	Creates a Grant Request from command line information and request send to SAS
	"""
	arr = []
	# Print registered nodes' CBSD ID's
	print("Registered CBSD IDs:")
	for node in registered_nodes:
		print("\t"+node.get_CbsdId())
		cbsdId = input("Enter CBSD of node you want to use for the grant request: ")
		operationParam = promptOperationParam()
		measReport = None
		provideRcvdPowerMeas = getSelectorBoolean(input("Do you want to provide Received Power Measurments to the SAS? (Y)es or (N)o: "))
		if(provideRcvdPowerMeas):
			rcvdReport = promptRcvdPowerMeasReport()
			measReport = MeasReport([rcvdReport])
		vtGrantParams = None
		provideVtGrantParams = getSelectorBoolean(input("Do you want to provide VT Grant Params? (Y)es or (N)o: "))
		if(provideVtGrantParams):
			vtGrantParams = promptVtGrantParams()
		arr.append(GrantRequest(cbsdId, operationParam, measReport, vtGrantParams).asdict())
	return arr

def grantRequest(clientio, payload=None):
	"""
	Creates a Grant Request and sends it to the SAS
	"""
	arrOfRequest = None
	if(__sim_mode):
		arrOfRequest = simGrantReq(payload)
	while(True):
		dataSource = input("Would you like to manually enter the Grant Request info or load from a file? (E)nter or (L)oad: ")
		if(dataSource == 'E' or dataSource == 'e'):
			arrOfRequest = cmdGrantReq()
			break
		elif(dataSource == 'L' or dataSource == 'l'):
			arrOfRequest = configGrantReq()
			break
		elif(dataSource == 'exit'):
			return
		else:
			print("Invalid Entry... Please enter 'E' for Manual Entry or 'L' to load from a config file...")
	payload = {"grantRequest": arrOfRequest}
	clientio.emit("grantRequest", json.dumps(payload))

def handleGrantResponse(clientio, data):
	"""
	Handles Grant Response message from SAS to CBSD
	"""
	jsonData = json.loads(data)
	iter = -1
	grantResponses = _grabPossibleEntry(jsonData, "grantResponse")
	if(not grantResponses):
		print("Unreadable data. Expecting JSON formatted payload. Grant(s) invalid.")
		return
	else:
		print("Grant Response Received")
	for grantResponse in grantResponses:
		iter = iter + 1
		print("Grant Response [" + str(iter+1) +"]:")
		response = _grabPossibleEntry(grantResponse, "response")
		if(not _isValidResponse(response)):
			operationParam = _grabPossibleEntry(grantResponse, "operationParam")
			if(operationParam):
				# If Grant is disapproved, this is the suggested operationParam for the Node
				print("Suggested Operation Parameters:")
				eirp = _grabPossibleEntry(operationParam, "maxEirp")
				freqRange = _grabPossibleEntry(operationParam, "operationFrequencyRange")
				if(freqRange):
					low = _grabPossibleEntry(freqRange, "lowFrequency")
					high = _grabPossibleEntry(freqRange, "highFrequency")
				if(low and high and eirp):
					print("Max EIRP: " + eirp)
					print("Operation Frequency Range: " + low + " - " +  high + " Hz")
			print("Grant invalid.")
			continue

		channelType = _grabPossibleEntry(grantResponse, "channelType")
		if(not channelType):
			print("No channelType provided. Grant invalid.")
			continue

		grantId = _grabPossibleEntry(grantResponse, "grantId")
		if(not grantId):
			print("No grantId provided. Grant invalid")
			continue
		
		grantExpireTime = _grabPossibleEntry(grantResponse, "grantExpireTime")
		if(not grantExpireTime):
			print("No grantExpireTime provided. Grant invalid.")
			continue

		heartbeatInterval = _grabPossibleEntry(grantResponse, "heartbeatInterval")
		if(not heartbeatInterval):
			print("No heartbeatInterval provided. Grant invalid.")
			continue

		cbsdId = _grabPossibleEntry(grantResponse, "cbsdId")
		if(cbsdId):
			node = findTempNodeByCbsdId(cbsdId)
			if(not node):
				print("No Node awaiting a response has the cbsdId '" + cbsdId +"'. Grant invalid.")
				continue
		else:
			print("SAS Error: No cbsdId provided. Grant invalid.")
			continue

		measReportConfig = _grabPossibleEntry(grantResponse, "measReportConfig")
		if(measReportConfig):
			if(not isinstance(measReportConfig, list)):
				measReportConfig = [measReportConfig]
			node.setMeasReportConfig(measReportConfig)

		nodeGrant = node.getGrant()
		nodeGrant.setGrantId(grantId)
		nodeGrant.setGrantStatus("GRANTED")
		nodeGrant.setGrantExpireTime(grantExpireTime)
		nodeGrant.setHeartbeatInterval(heartbeatInterval)
		nodeGrant.setChanneltype(channelType)
# End Grant Request------------------------------------------------------------------------

# Heartbeat Request------------------------------------------------------------------------
def simHeartbeatReq(requests):
	"""
	"""
	pass

def configHeartbeatReq():
	"""
	TODO
	"""
	return [None]

def cmdHeartbeatReq():
	"""
	Prompts user through creating a Heartbeat request
	"""
	arr = []
	cbsd = None
	# Print registered nodes' CBSD ID's
	print("Registered CBSD IDs:")
	for node in registered_nodes:
		print("\t"+node.get_CbsdId())
	cbsdId = input("Enter CBSD of node you want to use for the heartbeat request: ")
	for node in registered_nodes:
		if(node.get_CbsdId() == cbsdId):
			cbsd = node
	grantId = cbsd.get_GrantId() # TODO: error handle this
	grantRenew = getSelectorBoolean(input("Would you like to renew the grant? (Y)es or (N)o: "))
	operationState = input("Is this CBSD AUTHORIZED or GRANTED: ") # @Joseph Either 'AUTHORIZED' or 'GRANTED'. Which is which, I forget
	measReport = None
	provideRcvdPowerMeas = getSelectorBoolean(input("Do you want to provide Received Power Measurments to the SAS? (Y)es or (N)o: "))
	if(provideRcvdPowerMeas):
		rcvdReport = promptRcvdPowerMeasReport()
		measReport = MeasReport([rcvdReport])
	arr.append(HeartbeatRequest(cbsdId, grantId, grantRenew, operationState, measReport).asdict())
	return arr

def heartbeatRequest(clientio, payload=None):
	"""
	Creates a heartbeat request to send to the SAS
	"""
	if(__sim_mode):
		arrOfRequest = heartbeatRequest(payload)
	else:
		while(True):
			dataSource = input("Would you like to manually enter the Heartbeat Request info or load from a file? (E)nter or (L)oad: ")
			if(dataSource == 'E' or dataSource == 'e'):
				arrOfRequest = cmdHeartbeatReq()
				break
			elif(dataSource == 'L' or dataSource == 'l'):
				arrOfRequest = configHeartbeatReq()
				break
			elif(dataSource == 'exit'):
				return
			else:
				print("Invalid Entry... Please enter 'E' for Manual Entry or 'L' to load from a config file...")
	payload = {"heartbeatRequest": arrOfRequest}
	clientio.emit("heartbeatRequest", json.dumps(payload))

	# start timer to track how long it takes for the response to come in 
	# 240sec
	timeTilHearbeatExpires = 240 # seconds
	# or grant expire time, transmitExpirem, whichever is soonest
	global __heartbeatTimer
	#node_stop is the function to turn off the TX
	__heartbeatTimer = threading.Timer(timeTilHearbeatExpires, node_stop).start()

def handleHeartbeatResponse(clientio, data):
	"""
	Handles Heartbeat Response message from SAS to CBSD
	"""
	global __heartbeatTimer
	__heartbeatTimer.cancel()
	
	jsonData = json.loads(data)
	iter = -1
	hbResponses = _grabPossibleEntry(jsonData, "heartbeatResponse")
	if(not hbResponses):
		print("SAS Error: Unreadable data. Expecting JSON formatted payload. Heartbeat(s) invalid.")
		return
	else:
		print("Heartbeat Response Received")
	for hbResponse in hbResponses:
		iter = iter + 1
		print("Heartbeat Response [" + str(iter+1) +"]:")
		response = _grabPossibleEntry(hbResponse, "response")
		if(not _isValidResponse(response)):
			operationParam = _grabPossibleEntry(hbResponse, "operationParam")
			if(operationParam):
				# If Heartbeat is disapproved, this is the suggested operationParam for the Node
				print("Suggested Operation Parameters:")
				eirp = _grabPossibleEntry(operationParam, "maxEirp")
				freqRange = _grabPossibleEntry(operationParam, "operationFrequencyRange")
				if(freqRange):
					low = _grabPossibleEntry(freqRange, "lowFrequency")
					high = _grabPossibleEntry(freqRange, "highFrequency")
				if(low and high and eirp):
					print("Max EIRP: " + eirp)
					print("Operation Frequency Range: " + low + " - " +  high + " Hz")
			print("Heartbeat invalid.")
			continue

		cbsdId = _grabPossibleEntry(hbResponse, "cbsdId")
		if(cbsdId):
			node = findTempNodeByCbsdId(cbsdId)
			if(not node):
				print("No Node awaiting a response has the cbsdId '" + cbsdId +"'. Heartbeat invalid.")
				continue
		else:
			print("No cbsdId provided. Heartbeat invalid.")
			continue

		grantId = _grabPossibleEntry(hbResponse, "grantId")
		if(not grantId):
			print("No grantId provided. Heartbeat invalid")
			continue

		transmitExpireTime = _grabPossibleEntry(hbResponse, "transmitExpireTime")
		if(not transmitExpireTime):
			print("No transmitExpireTime provided. Heartbeat invalid.")
			continue

		grantExpireTime = _grabPossibleEntry(hbResponse, "grantExpireTime")
		if(not grantExpireTime):
			print("No grantExpireTime provided. Heartbeat invalid.")
			continue

		heartbeatInterval = _grabPossibleEntry(hbResponse, "heartbeatInterval")
		if(not heartbeatInterval):
			print("No heartbeatInterval provided. Heartbeat invalid.")
			continue
	
		measReportConfig = _grabPossibleEntry(hbResponse, "measReportConfig")
		if(measReportConfig):
			if(not isinstance(measReportConfig, list)):
				measReportConfig = [measReportConfig]
			node.setMeasReportConfig(measReportConfig)

		#if grantStatus == GRANTED, make it AUTH
		# Spawn 1 thread to wait for heartbeatInterval * 0.9 time
		# default time: 1 sec

		# Schedule the next HeartbeatRequest
		delayTilNextHeartbeat = float(heartbeatInterval) * 0.9
		if(delayTilNextHeartbeat < 1):
			delayTilNextHeartbeat = 1
		scheduleNextHeartbeat = threading.Timer(delayTilNextHeartbeat, heartbeatRequest)
		scheduleNextHeartbeat.start()
# End Heartbeat Request--------------------------------------------------------------------

# Relinquishment Request-------------------------------------------------------------------
def simRelinquishmentReq(requests):
	"""
	Read sim file for Relinquishment Request(s)
	"""
	arr = []
	global tempNodeList
	tempNodeList = []
	iter = 0
	for request in requests:
		print("Creating Relinquishment Request [" + str(iter+1) + "]:")
		node = reqAddressToNode(request)
		if(not node):
			print("No Created Node was found with the IP Address: '" + node.getIpAddress() + "'. Relinquishment Request invalid.")
			continue
		cbsdId = node.getCbsdId()
		if(not cbsdId):
			print("No cbsdId found for the node with IP Address: '" + node.getIpAddress() + "'. Relinquishment Request invalid.")
			continue
		grantId = node.getGrant().getGrantId()
		tempNodeList.append(node)
		arr.append(RelinquishmentRequest(cbsdId, grantId))
	return arr

def configRelinquishmentRequest():
	"""
	TODO
	"""
	return [None]		

def cmdRelinquishmentReq():
	"""
	Creates a Relinquishment Request and sends it to the SAS 
	"""
	arr = None
	cbsd = None
	# Print registered nodes' CBSD ID's
	print("Registered CBSD IDs:")
	for node in registered_nodes:
		print("\t"+node.get_CbsdId())
	cbsdId = input("Enter CBSD of node you want to use for the grant request: ")
	for node in registered_nodes:
		if(node.get_CbsdId() == cbsdId):
			cbsd = node
	grantId = cbsd.get_GrantId() # TODO: error handle this
	arr.append(RelinquishmentRequest(cbsdId, grantId).asdict())
	return arr

def relinquishmentRequest(clientio, payload=None):
	"""
	Creates Relinishment Request to send to the SAS
	"""
	while(True):
		dataSource = input("Would you like to manually enter the Relinquishment Request info or load from a file? (E)nter or (L)oad: ")
		if(dataSource == 'E' or dataSource == 'e'):
			arrOfRequest = cmdRelinquishmentReq()
			break
		elif(dataSource == 'L' or dataSource == 'l'):
			arrOfRequest = configRelinquishmentReq()
			break
		elif(dataSource == 'exit'):
			return
		else:
			print("Invalid Entry... Please enter 'E' for Manual Entry or 'L' to load from a config file...")
	payload = {"relinquishmentRequest": arrOfRequest}
	clientio.emit("relinquishmentRequest", json.dumps(payload))

def handleRelinquishmentResponse(clientio, data):
	"""
	Handles data returned from SAS regarding previously sent Relinquishment Request.

	Relinquishment Requests resets the Grant object for a Node
	"""
	jsonData = json.loads(data)
	iter = -1
	relinquishResponses = _grabPossibleEntry(jsonData, "relinquishmentResponse")
	if(not relinquishResponses):
		print("Unreadable data. Expecting JSON formatted payload. Relinquishment(s) invalid.")
		return
	else:
		print("Relinquishment Response Received")
	for relinquishment in relinquishResponses:
		iter = iter + 1
		print("Relinquishment Response [" + str(iter+1) +"]:")
		response = _grabPossibleEntry(relinquishment, "response")
		if(not _isValidResponse(response)):
			print("Relinquishment invalid.")
			continue

		cbsdId = _grabPossibleEntry(relinquishment, "cbsdId")
		if(cbsdId):
			node = findTempNodeByCbsdId(cbsdId)
			if(not node):
				print("No Node awaiting a response has the cbsdId '" + cbsdId +"'. Relinquishment invalid.")
				continue
		else:
			print("No cbsdId provided. Heartbeat invalid.")
			continue

		grantId = _grabPossibleEntry(relinquishment, "grantId")
		if(not grantId):
			print("No grantId provided. Relinquishment invalid")
			continue
		
		grant = node.getGrantRequest()
		grant.__init__() # TODO: Make sure this resets the Grant
		# TODO: If a heartbeat is scheduled, make sure to address it
# End Relinquishment Request---------------------------------------------------------------

# Deregistration Request-------------------------------------------------------------------
def simDeregistrationReq(requests):
	"""
	Read sim file for Deregistration Request(s)
	"""
	arr = []
	global tempNodeList
	tempNodeList = []
	iter = 0
	for request in requests:
		print("Creating Deregistration Request [" + str(iter+1) + "]:")
		node = reqAddressToNode(request)
		if(not node):
			print("No Created Node was found with the IP Address: '" + node.getIpAddress() + "'. Deregistration Request invalid.")
			continue
		cbsdId = node.getCbsdId()
		if(not cbsdId):
			print("No cbsdId found for the node with IP Address: '" + node.getIpAddress() + "'. Deregistration Request invalid.")
			continue
		tempNodeList.append(node)
		arr.append(DeregistrationRequest(cbsdId))
	return arr

def configDeregistrationReq():
	"""
	TODO
	"""
	return [None]

def cmdDeregistrationReq():
	"""
	Prompts user through creating a deregistration request
	"""
	arr = None
	cbsd = None
	# Print registered nodes' CBSD ID's
	print("Registered CBSD IDs:")
	for node in registered_nodes:
		print("\t"+node.get_CbsdId())
	cbsdId = input("Enter CBSD of node you want to use for the deregistration request: ")
	arr.append(DeregistrationRequest(cbsdId).asdict())
	return arr

def deregistrationReqest(clientio, payload=None):
	"""
	Creates a Deregistration request and sends it to the SAS
	"""
	if(__sim_mode):
		arrOfRequest = simDeregistrationReq(payload)
	else:
		while(True):
			dataSource = input("Would you like to manually enter the Deregistration Request info or load from a file? (E)nter or (L)oad: ")
			if(dataSource == 'E' or dataSource == 'e'):
				arrOfRequest = cmdDeregistrationReq()
				break
			elif(dataSource == 'L' or dataSource == 'l'):
				arrOfRequest = configDeregistrationReq()
				break
			elif(dataSource == 'exit'):
				return
			else:
				print("Invalid Entry... Please enter 'E' for Manual Entry or 'L' to load from a config file...")
	payload = {"deregistrationRequest": arrOfRequest}
	clientio.emit("deregistrationRequest", json.dumps(payload))

def handleDeregistrationResponse(clientio, data):
	"""
	Handles SAS Deregistration Response sent to CBSD
	"""
	jsonData = json.loads(data)
	for dereg in jsonData["deregistrationResponse"]:
		if(dereg["cbsdId"]):
			cbsdId = dereg["cbsdId"]
		if(dereg["response"]):
			response = dereg["response"]
# End Deregistration Request---------------------------------------------------------------


def stopNode(cbsdId):
	"""
	Takes CBSD ID and finds associated node and turns it off
	"""
	for node in created_nodes:
		if(node.get_CbsdId() == cbsdId):
			node.stop()
			return
	print("No node found with CBSD ID: " + cbsdId)

def startNode(cbsdId):
	"""
	Turns on the Node with the provided CBSD ID
	"""
	for node in created_nodes:
		if(node.get_CbsdId == cbsdId):
			node.start()
			return
	print("No node found with CBSD ID: " + cbsdId)


def defineSocketEvents(clientio):
	"""
	List of events the SAS may echo, and functions to call to handle them

	Parameters
	----------
	clientio : socketio Client object
		socket to SAS
	"""
	@clientio.event
	def connect():
		print('connection established. Given sid: ' + clientio.sid)

	@clientio.event
	def identifySource():
		clientio.emit("identifySource", ("I am CRTS"))
		# send_params(clientio, txUsrp)
		# registrationReq(clientio)

	# Official WinnForum Predefined Functionality
	@clientio.event
	def registrationResponse(data):
		global __blocked
		handleRegistrationResponse(clientio, data)
		__blocked = False

	@clientio.event
	def sprectumInquiryResponse(data):
		global __blocked
		handleSpectrumInquiryResponse(clientio, data)
		__blocked = False

	@clientio.event
	def grantResponse(data):
		global __blocked
		handleGrantResponse(clientio, data)
		__blocked = False

	@clientio.event
	def heartbeatResponse(data):
		global __blocked
		handleHeartbeatResponse(clientio, data)
		__blocked = False

	@clientio.event
	def relinquishmentResponse(data):
		global __blocked
		handleRelinquishmentResponse(clientio, data)
		__blocked = False

	@clientio.event
	def deregistrationResponse(data):
		global __blocked
		handleDeregistrationResponse(clientio, data)
		__blocked = False
	# end official WinnForum functions

	@clientio.event
	def getTxParams(node):
		send_params(clientio, node)

	@clientio.event
	def updateParams(cbsdId, newParams):
		updateRadio(cbsdId, newParams)

	@clientio.event
	def stop_radio(cbsdId):
		stopNode(cbsdId)

	@clientio.event
	def start_radio(cbsdId):
		startNode(cbsdId)

	# TODO - cbsdID and measReport
	# @clientio.event
	# def sendSpectrumData():
	# 	pass
	# @clientio.event
	# def operationParams(data):
	# 	pass

	@clientio.event
	def disconnect():
		print('Server terminated connection')

def init(args):
	"""
	Create radio object and connects to server

	Parameters
	----------
	args : list
		List of parameters extracted from command line flags
	"""

	clientio = socketio.Client()  # Create Client Socket
	defineSocketEvents(clientio)  # Create handlers for events the SAS may emit
	socket_addr = 'http://' + args['address'] +':' + args['port']
	clientio.connect(socket_addr) # Connect to SAS

	# Create global array of USRPs for use across functions
	# TODO: I should not have to reiterate what created_node is in here since I READ_ONLY in here
	# global created_nodes
	# created_nodes = []

	if(args['sim']):
		'''
		Simulation file includes all requests to make and at what times
		This requires no human interaction with the program. There may be output to read in the terminal.
		'''
		global __sim_mode
		__sim_mode = True
		path = args['sim']
		try:
			with open(path) as config:
				data = json.load(config)
		except FileNotFoundError:
			sys.exit("Fatal Error: No valid simulation file found at " + path + "\nExiting program...")
		for time in data: 				# Sim file may have multiple instances of time to trigger events
			for action in data[time]: 	# Each time may have multiple actions (requests)
				for func in action:		# Each requests may have multiple payloads
					print("Going to execute: " + func)
					payload = action[func]
					if(func == "createNode"):
						simCreateNode(payload)
					elif(func == "registrationRequest"):
						registrationRequest(clientio, payload)
					elif(func == "spectrumInquiryRequest"):
						spectrumInquiryRequest(clientio, payload)
					elif(func == "grantRequest"):
						grantRequest(clientio, payload)
					elif(func == "heartbeatRequest"):
						pass
					elif(func == "relinquishmentRequest"):
						pass
					elif(func == "deregistrationRequest"):
						pass
					#send payload to appropiate function
		
	else:
		# Main Menu
		# CMD is blocking sockets from printing until user enters another value
		# To remedy this, I may add a boolean that is True when the socket is busy 
		# Once the socket is done completeing the action the user entered, the bool
		# should allow the loop to proceed... TODO
		cmdCreateNode()
		global __blocked
		__blocked = False
		print("Enter 'h' for help/list of commands")
		while True:
			while not __blocked:
				userInput = input("User Input: ")
				if(userInput == 'h'):
					print("""Commands Include:
						0 - Exit Interface
						1 - Create USRP Node
						2 - View Created Nodes
						3 - Create Registration Request
						4 - Create Spectrum Inquiry Request
						5 - Create Grant Request
						6 - Create Heartbeat Request
						7 - Create Relinquishment Request
						8 - Create Deregistration Request
						""")
				elif(userInput == '0'):
					print("Exiting System...")
					sys.exit()
				elif(userInput == '1'):
					cmdCreateNode()
				elif(userInput == '2'):
					printCreatedNodes()
				elif(userInput == '3'):
					__blocked = True
					registrationRequest(clientio)
				elif(userInput == '4'):
					__blocked = True
					spectrumInquiryRequest(clientio)
				elif(userInput == '5'):
					__blocked = True
					grantRequest(clientio)
				elif(userInput == '6'):
					__blocked = True
					heartbeatRequest(clientio)
				elif(userInput == '7'):
					__blocked = True
					relinquishmentRequest(clientio)
				elif(userInput == '8'):
					__blocked = True
					deregistrationRequest(clientio)
		
	print("Exiting System...")
	sys.exit()

if __name__ == '__main__':
	args = vars(parser.parse_args())	# Get command line arguments
	init(args)							# Init Tx USRP and Socket
	