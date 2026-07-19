from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, BooleanField, SubmitField
from wtforms.validators import DataRequired, Email, Length


class LoginForm(FlaskForm):
    identifier = StringField("Email or Username", validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired()])
    remember = BooleanField("Remember me")
    submit = SubmitField("Log In")


class EmailCodeRequestForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    submit = SubmitField("Send Code")


class EmailCodeVerifyForm(FlaskForm):
    code = StringField("Login Code", validators=[DataRequired(), Length(min=6, max=6, message="Enter the 6-digit code.")])
    submit = SubmitField("Verify")
