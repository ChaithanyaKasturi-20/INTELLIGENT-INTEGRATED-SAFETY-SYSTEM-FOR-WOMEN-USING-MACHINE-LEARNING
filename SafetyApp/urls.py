from django.urls import path

from . import views

urlpatterns = [path("index.html", views.index, name="index"),
	             path("UserLogin.html", views.UserLogin, name="UserLogin"),
		     path("UserLoginAction", views.UserLoginAction, name="UserLoginAction"),
		     path("Register.html", views.Register, name="Register"),
		     path("RegisterAction", views.RegisterAction, name="RegisterAction"),
		     path("TrainML", views.TrainML, name="TrainML"),
		     path("Route", views.Route, name="Route"),
		     path("RouteAction", views.RouteAction, name="RouteAction"),
		     path("CrimePredict", views.CrimePredict, name="CrimePredict"),
		     path("CrimePredictAction", views.CrimePredictAction, name="CrimePredictAction"),
		     path("Panic", views.Panic, name="Panic"),
		     path("PanicAction", views.PanicAction, name="PanicAction"),	
		     path("Heatmap", views.Heatmap, name="Heatmap"),
		    ]